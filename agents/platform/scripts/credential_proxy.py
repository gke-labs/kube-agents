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
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping

import command_policy
import providers
import scoped_sa_pool
import vcs_broker

# Re-exported, not re-implemented. The shim owns kubeconfig parsing because the
# file is in its pod and not in this one; these three are the vocabulary both
# sides share, and importing them keeps the context-name grammar in one place.
# Nothing else in credential_proxy_client runs on import.
from credential_proxy_client import (  # noqa: F401  (re-export)
    ClusterTarget,
    parse_gke_context,
    read_current_context,
)

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


def _redacted_fields(exc) -> dict:
    """A forge refusal, with anything token-shaped taken out of it.

    A `WorkspaceError` from a forge can carry the CLI's or the API's own words
    in `detail` -- that is the point of it, the caller needs to know what the
    forge said -- and those words crossed back into the sandbox verbatim. Every
    other route out of this process runs its subprocess output through
    `redact_credentials` first, and this one is the same risk with a shorter
    path: the sandbox is the side that must not learn a credential.

    Applied here rather than in `providers`, because the shapes the redactor
    matches are one forge's token formats and no module under `providers/` may
    name a forge. Applying it at the boundary also means a forge added later
    gets it without having asked.
    """
    fields = {key: value for key, value in exc.fields.items()}
    for key, value in fields.items():
        if isinstance(value, str):
            fields[key] = redact_credentials(value)
    return {"error": redact_credentials(str(exc)), **fields}


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
# the namespace can replay it until it expires.  mTLS closes that and is not
# done here.  buildCredentialProxyNetworkPolicy narrows who can open the
# connection at all, to the sandbox Pod and the gateway Pod.
# ---------------------------------------------------------------------------

DEFAULT_CREDENTIAL_PROXY_AUDIENCE = "kubeagents-credential-proxy"

# The second audience, and the whole of the per-caller split.
#
# Two Pods call this broker and ``Principal.workload`` cannot tell them apart.
# It is per-ServiceAccount, and the two ServiceAccounts are both on
# CREDENTIAL_PROXY_ALLOWED_CALLERS, so knowing which one called says only that
# the caller was one of the two Pods entitled to. What *can* separate them is
# the audience their token was projected with: the operator chooses it per Pod,
# and the API server refuses to validate a token against an audience it was not
# minted for. So the gateway's token is minted for the chat audience and the
# sandbox's for the audience above, and the routes each may reach follow from
# whichever one the TokenReview echoed back.
#
# The split is real rather than notional because the two callers already need
# disjoint routes and the operator already gives each only what its side needs:
# the gateway has GOOGLE_CHAT_RELAY_URL and SLACK_RELAY_URL and an empty
# CREDENTIAL_PROXY_URL, and the sandbox has the reverse. What was missing was
# anything on this side that refused when a caller reached across.
#
# Enforced here rather than by splitting the listener across two ports and
# letting the NetworkPolicy sort them out, for one reason:
# buildCredentialProxyNetworkPolicy is inert on a cluster whose CNI does not
# implement NetworkPolicy, and the API server's TokenReview is not. A port
# split would have been a control on some clusters and a comment on the rest.
DEFAULT_CREDENTIAL_PROXY_CHAT_AUDIENCE = "kubeagents-credential-proxy-chat"

# The roles a caller can hold, named by which Pod holds them.
CALLER_ROLE_SHELL = "shell"
CALLER_ROLE_CHAT = "chat"

# Which role each route demands. Checked by prefix, so the trailing slash on
# the three families is load-bearing: without it "/v1/chatter" would match
# "/v1/chat" and inherit its rule.
#
# A route absent from this table is reachable by any authenticated caller.
# That is the right default for the two that are: /healthz, which the readiness
# probe reaches before any token exists, and an unknown path, which must answer
# 404 to the caller that may legitimately be probing for it —
# credential_proxy_client.workspaces_available detects an older broker by
# asking, and a 403 there would read as "not permitted" rather than "not
# supported".
ROUTE_ROLES: tuple[tuple[str, str], ...] = (
    ("/v1/chat/", CALLER_ROLE_CHAT),
    ("/v1/exec", CALLER_ROLE_SHELL),
    ("/v1/forge/", CALLER_ROLE_SHELL),
    ("/v1/github/", CALLER_ROLE_SHELL),
    ("/v1/vcs/", CALLER_ROLE_SHELL),
    ("/v1/workspace/", CALLER_ROLE_SHELL),
)


def required_role(path: str) -> str:
    """The caller role ``path`` demands, or "" if it demands none."""
    for prefix, role in ROUTE_ROLES:
        if path.startswith(prefix):
            return role
    return ""


# How long a managed-repository allowlist read is reused.
#
# The list arrives as a ConfigMap mounted read-only at GITOPS_STATE_PATH, and
# kubelet refreshes such a mount on its own schedule -- around a minute, and not
# promptly. So there is already a window between registering a repository and
# this Pod seeing it, and a cache shorter than that window buys nothing but
# syscalls. Thirty seconds keeps the added delay well inside the one the mount
# imposes anyway.
MANAGED_REPOSITORY_CACHE_SECONDS = 30.0

_managed_repository_cache: tuple[float, frozenset[str]] | None = None
_managed_repository_lock = threading.Lock()


def managed_repositories() -> frozenset[str]:
    """The `owner/name` slugs this install is configured to act on, lowercased.

    Read from the same mounted ConfigMap `github_token_refresh` already reads to
    widen token scoping, through the same helper, so there is one parser and one
    notion of what counts as a managed GitHub repository.

    Raises rather than returning empty when the list cannot be read. The two
    outcomes are not the same: an empty list is an install with nothing
    registered, which is a legitimate state that refuses every repository, and
    an unreadable one is a broker that does not know what it is allowed to do.
    Returning empty for both would make them indistinguishable in the log at the
    moment an operator most needs to tell them apart.
    """
    global _managed_repository_cache
    now = time.monotonic()
    with _managed_repository_lock:
        cached = _managed_repository_cache
        if cached is not None and cached[0] > now:
            return cached[1]
    from gitops_workspace import get_managed_github_repos

    slugs = frozenset(slug.lower() for slug in get_managed_github_repos())
    with _managed_repository_lock:
        _managed_repository_cache = (now + MANAGED_REPOSITORY_CACHE_SECONDS, slugs)
    return slugs


def repository_is_managed(repository: str) -> bool:
    """Is ``repository`` one this install registered?

    Compared case-insensitively because GitHub treats owner and repository names
    that way, and the two sides of this comparison are written by different
    people: the slug in the request comes from a git remote or a model, and the
    one in the ConfigMap from whoever registered it.
    """
    return repository.lower() in managed_repositories()


def require_managed_workspace(store, handle: object) -> None:
    """Refuse a workspace write to a repository this install does not manage.

    `validate_repo` and `get_managed_github_repos` moved into skill scripts that
    now run in the sandbox, which makes them advice the agent gives itself
    rather than a control. The broker holds the installation token, so the
    question "is this a repository we write to" has to be answered here.
    `CredentialProxyHandler._repository_is_permitted` is the same check on the
    GitHub API routes; this one raises instead of writing a reply, because the
    workspace routes answer through the `ContentWorkspaceError` family.

    On `commit` and `push` rather than on `open`: opening is a read, and
    `inspect-repository` opens repositories this install does not manage on
    purpose. The repository comes off the handle rather than off the request, so
    a caller cannot name one repository and write to another.

    An unreadable list refuses rather than allows -- an authorization check that
    fails open is not one -- and says which of the two it was in the log.
    """
    import content_workspace

    repository = store.get(handle).repo
    try:
        permitted = repository_is_managed(repository)
    except Exception as exc:
        LOGGER.warning(
            "refusing a workspace write: the managed-repository list could not "
            "be read type=%s",
            type(exc).__name__,
        )
        raise content_workspace.ManagedRepositoriesUnavailable(
            "the managed repository list is unavailable"
        ) from exc
    if not permitted:
        raise content_workspace.RepositoryNotManaged(
            f"{repository} is not one of the repositories this agent manages; "
            "register it in the gitops-state ConfigMap first"
        )


# Chat API methods the relay refuses to spend its credential on.
#
# A denylist rather than an allowlist, for the reason the command policy below
# gives at "A denylist rather than a read-only allowlist, deliberately": the
# resource tree these names index belongs to the Hermes adapter and the Google
# Chat discovery document, neither of which is in this repository, so an
# allowlist would be enumerated by reading an image we do not build. A name
# missed out of a denylist is a call that still works; a name missed out of an
# allowlist is chat down, and chat is the front door.
#
# What is on it is the set whose effect cannot be undone by sending another
# message: removing a space, removing a member, deleting a message or a
# reaction. Reads and writes stay open, because the relay's whole purpose is
# for the agent to read and answer chat.
#
# Case-folded on comparison. googleapiclient resolves method names exactly, so
# a differing case would 404 upstream rather than execute -- but the check is
# an authorization decision and should not depend on that being true.
DESTRUCTIVE_CHAT_METHODS = frozenset({"delete", "batchdelete", "remove", "purge"})

# The same, for Slack, whose API is flat `group.verb` strings rather than a
# resource tree. Matched on the verb after the last dot so that a family added
# upstream -- `bookmarks.remove` after `chat.delete` -- is covered without this
# list naming it.
DESTRUCTIVE_SLACK_VERBS = frozenset({"delete", "remove", "kick", "archive"})


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

    ``role`` is the coarse version of that idea which does hold today, and it
    comes from the same place: the audience the API server validated the token
    against, which the operator sets per Pod. It says which *side* is calling —
    the shell or the chat gateway — and that is enough to keep either from
    reaching the other's routes. It is not per-session and does not pretend to
    be. "" means no role was established, which is the ``NullAuthenticator``
    case and reaches every route, because that authenticator is only sound
    behind a Unix socket where the filesystem is the access control.
    """

    workload: str
    uid: str = ""
    groups: tuple[str, ...] = ()
    caller: str | None = None
    role: str = ""

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

    ``audience_roles`` maps each audience this broker accepts to the caller role
    it confers. The TokenReview asks about all of them at once and the API
    server echoes back only those it actually validated, so the role is read off
    the answer rather than guessed from the request. A projected token carries
    exactly one audience, so exactly one can come back; more than one is a
    disagreement with that assumption rather than a wider grant, and is refused.
    """

    authenticates = True

    def __init__(
        self,
        audience_roles: Mapping[str, str],
        allowed_callers: frozenset[str],
        api_host: str,
        api_port: str,
        ca_file: str,
        token_file: str,
        timeout_seconds: float = 10.0,
        cache_seconds: float = 60.0,
    ) -> None:
        if not audience_roles or not all(audience_roles):
            raise ValueError("an audience is required to authenticate callers")
        if not allowed_callers:
            raise ValueError("at least one allowed caller is required")
        if not api_host:
            raise ValueError("the Kubernetes API server address is not configured")
        self.audience_roles = dict(audience_roles)
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
                "spec": {"token": token, "audiences": sorted(self.audience_roles)},
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
        # The API server echoes the audiences it actually validated. A token it
        # authenticated for some other audience is not for us, and one it
        # validated for two of ours breaks the assumption the role rests on.
        audiences = status.get("audiences") or []
        matched = sorted(
            {
                audience
                for audience in audiences
                if isinstance(audience, str) and audience in self.audience_roles
            }
        )
        if not matched:
            raise AuthenticationError("the presented token is for another audience")
        if len(matched) > 1:
            raise AuthenticationError(
                "the presented token names more than one of this broker's audiences"
            )
        user = status.get("user") or {}
        username = user.get("username") or ""
        if username not in self.allowed_callers:
            raise AuthenticationError("the authenticated caller is not permitted")
        groups = user.get("groups") or []
        return Principal(
            workload=username,
            uid=str(user.get("uid") or ""),
            groups=tuple(str(group) for group in groups if isinstance(group, str)),
            role=self.audience_roles[matched[0]],
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
    shell_audience = os.getenv(
        "CREDENTIAL_PROXY_AUDIENCE", DEFAULT_CREDENTIAL_PROXY_AUDIENCE
    ).strip()
    # Absent means "no split", and that is the whole of the upgrade story.
    #
    # A broker on this image rendered by an operator that predates the split
    # sees one audience, and every caller presenting it gets role "" — which
    # reaches every route, exactly as it did before this existed. Were the
    # second audience defaulted instead, that broker would hand the gateway the
    # shell role and answer 403 to every chat call, and an upgrade that rolls
    # the broker before the operator would take chat down until it caught up.
    #
    # This is why the value is read raw rather than through a default: unset and
    # set-to-the-default have to be distinguishable, and after os.getenv applies
    # a default they are not.
    chat_audience = os.getenv("CREDENTIAL_PROXY_CHAT_AUDIENCE", "").strip()
    if chat_audience and chat_audience != shell_audience:
        audience_roles = {
            shell_audience: CALLER_ROLE_SHELL,
            chat_audience: CALLER_ROLE_CHAT,
        }
    else:
        audience_roles = {shell_audience: ""}
    return ServiceAccountAuthenticator(
        audience_roles=audience_roles,
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
        if method.lower() in DESTRUCTIVE_CHAT_METHODS:
            raise ValueError(
                f"the Google Chat method {method!r} is not available through the relay"
            )
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
        if method.rpartition(".")[2].lower() in DESTRUCTIVE_SLACK_VERBS:
            raise ValueError(
                f"the Slack method {method!r} is not available through the relay"
            )
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
    # The kubeconfig `gcloud container clusters get-credentials` just wrote,
    # returned to the caller because the caller is in another pod and gcloud
    # ran in this one. Empty for every other command. See
    # `_execute_get_credentials`.
    kubeconfig: str = ""


# A kubeconfig is not passive data. `users[].user.exec.command` runs a program
# wherever the file is opened; `clusters[].cluster.server` and `proxy-url` choose
# where the access token minted by gke-gcloud-auth-plugin is sent;
# `users[].user.tokenFile` reads a file of the author's choosing and sends it as
# the bearer token. The policy engine cannot see any of that, because every rule
# it holds matches on argv and the argv is only ever `kubectl get pods`.
#
# So the broker never opens one. The agent's kubeconfig is in the agent's own
# pod, the shim there reads the single string that says which cluster is wanted
# (`credential_proxy_client.kubeconfig_context`), and that name is what arrives
# on the wire. Everything else is regenerated by `gcloud container clusters
# get-credentials`. `ClusterTarget`, `parse_gke_context` and
# `read_current_context` live with the shim for the same reason: the parsing
# happens where the file is, which is not here.


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
# this closes. `config`, `remote` and every read verb are untouched.
#
# `pull`, `submodule` and `sparse-checkout` are here because each one is a
# working-tree write wearing another word: `pull` is `fetch` plus the `merge`
# or `rebase` two lines up, `submodule update` checks out whole directories,
# and `sparse-checkout set` adds and removes files across the entire tree. All
# three were reachable in a clone another agent was midway through.
#
# `clone` and `fetch` were left out at first, on the argument that neither
# writes a working tree it does not own. `fetch` does something worse: it moves
# `origin/*` in whatever clone it is run in, and every lease-holder in this
# product compares against those refs to decide whether its work raced someone
# else's. A foreign fetch makes that comparison agree while the answer is
# wrong. `clone` writes into a destination it does not choose, which can be a
# directory inside another agent's lease. Both are leased today by every caller
# that issues them — `ensure_workspace` writes the marker before it clones, at
# the lease root the clone runs in — so requiring the lease costs nothing and
# closes the two remaining ways one agent reaches another's tree.
GIT_MUTATING_SUBCOMMANDS = frozenset(
    {
        "add", "am", "apply", "branch", "checkout", "cherry-pick", "clean",
        "clone", "commit", "fetch", "merge", "mv", "pull", "push", "rebase",
        "reset", "restore", "revert", "rm", "sparse-checkout", "stash",
        "submodule", "switch", "tag", "update-ref", "worktree",
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

# Where the version-control routes build and delete their scratch trees. A
# sibling of the content workspaces rather than a subdirectory: the two are
# contained separately, and a shared parent would make one path's containment
# check accept the other's directories.
VCS_SCRATCH_DIR = "vcs"

# The git subcommands the version-control broker issues on its own behalf. A
# closed list checked against the argv as parsed, so a later edit that threads
# a caller's string into a new vector is refused rather than run.
#
# `checkout` is on it for one caller, on the read path: `clone` puts the named
# branch on HEAD before writing the bundle, because a clone that landed on the
# remote's default branch would bundle the wrong revision. Nothing on the write
# path needs it -- an incoming bundle is unbundled, inspected and pushed without
# its objects ever being materialised into a working tree.
#
# `update-ref` is on the list for one caller: `bundle unbundle` puts the
# objects in the store and prints the refs but writes none, so the broker names
# the incoming tip itself. It is the narrower half of the alternative -- the
# other way to read a bundle is `fetch <path>`, which is the `file` transport
# `GIT_ALLOW_PROTOCOL` refuses everywhere for reasons the executor environment
# spells out.
VCS_GIT_SUBCOMMANDS = frozenset(
    {
        "bundle",
        "checkout",
        "clone",
        "fetch",
        "init",
        "ls-remote",
        "merge-base",
        "push",
        "remote",
        "rev-parse",
        "symbolic-ref",
        "update-ref",
    }
)

# Where a credential refresh helper is staged. A forge's helper is found by its
# own name under this directory, which is how the generic route reaches a
# provider-specific operation without this file listing providers.
FORGE_REFRESH_HELPER_DIR = "/opt/defaults/scripts"

# What may be spliced into that filename. Closed, anchored and lowercase: a
# provider name reaching a path is the one place a forge's own string could
# become a directory traversal.
_PROVIDER_RE = re.compile(r"[a-z][a-z0-9]{0,31}")

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


# Distinguishes "the caller said None" from "the caller said nothing" for
# `scoped_pool`. None is a real, meaningful value there — it is the ambient
# credential — so a plain default of None would make an un-parameterised
# construction silently opt out of the pool, which is the one behaviour this
# increment cannot afford to reach by omission.
_FROM_ENVIRONMENT = object()


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


_forge_registry: providers.Registry | None = None
_forge_registry_lock = threading.Lock()


def forge_registry() -> providers.Registry:
    """The forges this install has, for the questions that need no credential.

    Resolving a repository spec and refusing an unserved host are pure
    functions of the host table, so the routes that only need to *identify* a
    repository share one registry rather than each building their own. The
    broker keeps its own, constructed with the refresh operation, because that
    one is the object that spends something.
    """
    global _forge_registry
    with _forge_registry_lock:
        if _forge_registry is None:
            _forge_registry = providers.Registry()
        return _forge_registry


def broker_executables() -> tuple[str, ...]:
    """What the credentialed process may run at all.

    Two lists exist and they are not the same list. This one says what may run
    *here*, in the container that holds the token;
    `credential_proxy_client.SUPPORTED_EXECUTABLES` says what the sandbox may
    ask to have run on its behalf. They used to be the same four names, which
    read as one decision and was two.

    `gcloud` and `kubectl` are on both: the agent names them and this process
    runs them. `git` is only here -- the broker issues it on its own behalf and
    the agent reaches version control by verb. And a forge CLI is here only if
    some forge this install built declares one, so an install whose forges all
    speak HTTP grants no forge binary rather than inheriting the union of every
    binary any forge could want.
    """
    return ("gcloud", "kubectl", "git", *providers.Registry().executables)


class CommandExecutor:
    ALLOWED_EXECUTABLES = broker_executables()

    def __init__(
        self,
        timeout_seconds: int,
        max_output_bytes: int,
        state_dir: str,
        scoped_pool: "scoped_sa_pool.ScopedServiceAccountPool | None | object" = _FROM_ENVIRONMENT,
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
        # Always present, unlike the content-workspace root. Version control is
        # not behind a switch -- there is no other way for the sandbox to reach
        # a repository -- so the directory its scratch trees live in exists on
        # every start, and `execute_vcs_git` is reachable whenever the process
        # is. Resolved for the same reason as the roots above: `_execute`
        # compares it against a resolved `cwd`, and an unresolved root under a
        # symlinked prefix refuses every legitimate call.
        self.vcs_root = (self.state_dir / VCS_SCRATCH_DIR).resolve()
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
            self.vcs_root,
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
        # Built last: `build_pool` raises on a mapping that is armed and
        # unusable, and failing here means the container never serves a request
        # under the ambient credential while an operator believes it is scoped.
        self.scoped_pool = (
            scoped_sa_pool.build_pool()
            if scoped_pool is _FROM_ENVIRONMENT
            else scoped_pool
        )
        if self.scoped_pool is not None:
            LOGGER.info(
                "scoped service account pool armed scopes=%d", len(self.scoped_pool.scopes)
            )

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
        kubeconfig_context: str | None = None,
        wants_kubeconfig: bool = False,
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
            return self._execute_get_credentials(command, stdin, cwd, wants_kubeconfig)

        # Two ways in, and both have to be covered or the other is a bypass.
        # `--kubeconfig` predates the KUBECONFIG forward and takes precedence
        # over it in kubectl, so closing only the environment would leave the
        # flag as an open door.
        #
        # Only kubectl reaches pool selection, and the gate is here rather
        # than inside the pool: the client forwards KUBECONFIG for gcloud too
        # (credential_proxy_client.KUBECONFIG_AWARE), and an agent always has
        # one exported, so without the gate every gcloud read would be
        # refused or would mint for a variable gcloud never reads. Non-kubectl
        # requests still resolve a named kubeconfig the way they did before
        # the pool existed -- regenerated on the ambient identity, never
        # selected on.
        scoped = executable == "kubectl"
        command, flag_kubeconfig = self._reroute_kubeconfig_flags(command, scoped=scoped)
        if flag_kubeconfig is not None:
            # The flag beats the environment, because that is the precedence
            # kubectl itself applies -- and the reroute above has already put
            # the flag's cluster through selection. Resolving the forwarded
            # environment kubeconfig as well would select a *second* cluster
            # for a request the flag has pinned: with the environment's
            # cluster unmapped that is a refusal of a request naming a cluster
            # the pool covers, and with it mapped it is a second token minted
            # and thrown away. Neither is a control, so the environment file
            # is not resolved at all when a flag is present.
            #
            # The environment follows the flag when the pool is armed so the
            # two cannot disagree, and is left alone otherwise, which is what
            # the flag path did before the pool existed.
            kubeconfig_path = (
                flag_kubeconfig if self.scoped_pool is not None and scoped else None
            )
        elif kubeconfig_context:
            kubeconfig_path = self._resolve_kubeconfig(kubeconfig_context, scoped=scoped)
        elif self.scoped_pool is not None and executable == "kubectl":
            # `KUBECONFIG` is in the base environment, so this branch is not
            # "no cluster" — it is "the sidecar's default cluster", and it has to
            # go through selection like any other.
            #
            # Only kubectl. gcloud names its target in argv rather than in a
            # kubeconfig, and deciding scope from argv would put a parser where
            # the boundary belongs. So gcloud, git and gh keep running as the
            # agent's own identity, and what bounds them is that identity's
            # remaining IAM rather than anything decided here.
            #
            # Do not read that as "kubectl is the only way to reach a Kubernetes
            # object." It is not, and the difference matters. The `gke` remote
            # MCP server in every profile's config.yaml proxies to
            # container.googleapis.com/mcp from the *agent* container, on the
            # ambient Workload Identity credential, with no part of this file in
            # the path. Nothing here scopes it and nothing here can.
            #
            # What scopes it is the size of the agent's own grant — which is why
            # taking roles/container.viewer off that identity is not a tidy-up
            # alongside this work but the half of it that covers this door.
            kubeconfig_path = self._default_kubeconfig()
        else:
            kubeconfig_path = None
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

        * the subcommand is one of the twelve this product issues, checked
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

    def execute_vcs_git(
        self,
        argv: list[str],
        cwd: Path,
        check: bool = True,
        config: tuple[tuple[str, str], ...] = (),
    ) -> subprocess.CompletedProcess:
        """git the version-control broker issues, in its own scratch tree.

        A third door rather than a widening of the second. The broker needs
        `bundle`, `init`, `remote` and `ls-remote`, which content-passing does
        not, and putting them on one list would grant each path the other's
        subcommands for no reason beyond sharing a method.

        `config` is what the forge's credential asked for on this invocation --
        a helper pin, an `insteadOf`, whatever presenting that forge's
        credential to git takes. It goes into the `GIT_CONFIG_COUNT` layer
        *before* the forced pins, so a forge cannot turn off hooks containment
        or GPG program pinning by asking for the same key.

        Answers as a `CompletedProcess` because that is what the broker's
        callers read, and raises `CalledProcessError` when `check` is set --
        the same contract `subprocess.run` has, so the broker's logic reads as
        ordinary git plumbing rather than as an executor protocol.
        """
        if not argv or argv[0] != "git":
            raise ValueError("only git runs on the version-control path")
        executable_path = self.executables.get("git")
        if not executable_path:
            raise RuntimeError("supported executable is unavailable: git")
        subcommand, redirects = _git_plan(argv)
        if redirects:
            raise ValueError("`-C` is not accepted on the version-control path")
        if subcommand not in VCS_GIT_SUBCOMMANDS:
            raise ValueError(
                f"`git {subcommand}` is not one of the subcommands the "
                "version-control broker issues on its own behalf"
            )
        result = self._execute(
            [executable_path, *argv[1:]],
            cwd=str(cwd),
            containment_root=self.vcs_root,
            extra_config=tuple(config),
        )
        if check and result.exit_code != 0:
            raise subprocess.CalledProcessError(
                result.exit_code, argv, result.stdout, result.stderr
            )
        return subprocess.CompletedProcess(
            argv, result.exit_code, result.stdout, result.stderr
        )

    def execute_forge_cli(
        self, argv: list[str], stdin: str | None = None
    ) -> subprocess.CompletedProcess:
        """Run a forge's CLI, from a directory that holds no repository.

        The counterpart of `execute_vcs_git` for the collaboration verbs, and it
        exists for the same reason: `_execute` is where the credential
        environment is assembled, and a second copy of that assembly would
        drift from the first.

        The working directory is the scratch root itself, deliberately. A forge
        CLI shells out to git and infers a repository from whatever
        `.git/config` it can find above the cwd, so running it inside one of the
        scratch clones would let a config that arrived in a caller's bundle
        decide what the credentialed process does. Every call the broker makes
        names an explicit API path, so it needs no repository at all.

        `stdin` carries the request body. Not argv: what a caller wrote must not
        be visible in `ps` or reappear inside a `CalledProcessError` that some
        layer above logs -- the same argument `_execute`'s own stdin handling
        already makes for the installation token.

        Not reachable from `/v1/exec`. The argv is composed in `vcs_broker`, the
        subcommand is always the CLI's API passthrough, and the only
        caller-supplied strings in it are validated fields.
        """
        if not argv:
            raise ValueError("a forge CLI invocation needs an executable")
        executable_path = self.executables.get(argv[0])
        if not executable_path:
            raise RuntimeError(f"supported executable is unavailable: {argv[0]}")
        self.vcs_root.mkdir(parents=True, exist_ok=True)
        result = self._execute(
            [executable_path, *argv[1:]],
            stdin=stdin,
            cwd=str(self.vcs_root),
            containment_root=self.vcs_root,
        )
        return subprocess.CompletedProcess(
            argv, result.exit_code, result.stdout, result.stderr
        )

    def refresh_forge_credential(self, provider: str, repository: str) -> None:
        """Make this install's credential for `repository` current, or raise.

        The privileged operation a `BrokeredCredential` names and does not
        perform. Which forge is asking arrives as an argument rather than being
        decided here, and the helper that does the work is found by the
        provider's own name -- so a second forge that needs a brokered
        credential ships a helper and edits nothing in this file.

        The provider is matched against a closed grammar before it reaches a
        path. It comes from a forge class rather than from a request today, and
        the check is what keeps that true if a route ever passes one through.

        Whether the repository is one this install acts on is settled here too,
        for the reason `_repository_is_permitted` gives: this is the call that
        spends the token, so it is the call that has to ask.
        """
        if not _PROVIDER_RE.fullmatch(provider or ""):
            raise ValueError("provider is not a forge name")
        if not repository_is_managed(repository):
            raise PermissionError(f"{repository} is not a repository this install manages")
        helper = Path(FORGE_REFRESH_HELPER_DIR) / f"{provider}_token_refresh.py"
        if not helper.is_file():
            # An absent helper is a refusal rather than a no-op. A credential
            # strategy that asked to be made current and silently was not is a
            # 401 later, from inside a clone, that reads like the repository is
            # gone.
            raise RuntimeError(f"no credential refresh helper for {provider}")
        result = self.execute_internal([str(helper), repository])
        if result.exit_code != 0:
            # Logged here and not returned: the detail crosses back into the
            # sandbox otherwise, and it is the one place a broker outage is
            # diagnosable. Redacted before it is bounded, so a token cut in half
            # by the slice is not what survives.
            detail = redact_credentials(result.stderr.strip())
            LOGGER.warning(
                "%s credential refresh exited %d%s",
                provider,
                result.exit_code,
                f": {detail[:1000]}" if detail else "",
            )
            raise RuntimeError("credential refresh failed")

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

    def _resolve_kubeconfig(self, context: str, *, scoped: bool = True) -> Path:
        """Turn the cluster name a caller sent into a kubeconfig the proxy wrote.

        A name is all that arrives. The shim in the agent's pod reads
        `current-context` out of the kubeconfig there and sends that string
        (`credential_proxy_client.kubeconfig_context`), so no document the agent
        authored is ever opened on this side — the `exec` stanza, `auth-provider`,
        `server`, `proxy-url`, `tokenFile` and `insecure-skip-tls-verify` are all
        written by gcloud rather than by the agent, and there is no allowlist to
        keep current.

        The name is still checked here rather than trusted, because the shim is
        on the far side of the wire and everything that crosses it is caller
        input: `parse_gke_context` is what keeps this value out of a filename
        and a log line it has no business in.

        What the caller keeps is the ability to *name* a cluster. That is not new
        authority: `get-credentials` is bound by the same IAM the proxy already
        runs under, so it can only name clusters this identity could reach anyway.
        """
        target = parse_gke_context(context.strip())
        if target is None:
            raise ValueError(
                f"kubeconfigContext {context!r} is not a GKE context name"
                " (expected gke_<project>_<location>_<cluster>)"
            )
        return self._kubeconfig_for(target, scoped=scoped)

    def _kubeconfig_for(self, target: ClusterTarget, *, scoped: bool = True) -> Path:
        """Swap the ambient credential for the one that only reads this cluster.

        The managed kubeconfig authenticates with gke-gcloud-auth-plugin, which
        resolves Application Default Credentials — the agent's own service
        account, whose IAM reaches every cluster in the project. When the pool is
        armed that is replaced by a token minted for the account this cluster
        maps to, and a cluster with no account is refused rather than served by
        the wide one.

        Selection happens *before* `_ensure_managed_kubeconfig`, and the order is
        the point. That call runs `gcloud container clusters get-credentials`
        against the named cluster on the ambient identity; doing it first would
        mean an unmapped cluster still produced a live call to GKE on the wide
        credential before the refusal, and would make the refusal depend on that
        call having succeeded. Refusing first costs nothing and keeps the two
        independent.

        The scoped file sits beside the managed one in the sidecar-only state
        dir, and it is rewritten on every call rather than cached: the token
        behind it rotates, and a file that outlives its token fails as an
        authentication error somewhere far from here.
        """
        if self.scoped_pool is None or not scoped:
            # Not scoped: a non-kubectl request that named a kubeconfig. The
            # file is still regenerated -- the name-not-content property does
            # not depend on the pool -- but on the ambient identity, exactly
            # as before the pool existed, because only kubectl reads the
            # credential this file carries.
            return self._ensure_managed_kubeconfig(target)
        token = self.scoped_pool.token_for(target.project, target.location, target.cluster)
        managed = self._ensure_managed_kubeconfig(target)
        scoped = self.kubeconfig_dir / f"{target.context_name}.scoped.yaml"
        document = scoped_sa_pool.kubeconfig_with_token(
            managed.read_text(encoding="utf-8"), token
        )
        scratch = self.kubeconfig_dir / f".scoped-{uuid.uuid4().hex}.yaml"
        try:
            # Created 0600 by the open itself. Writing then chmod-ing would leave
            # a window in which a bearer token for a cloud identity is readable
            # at whatever the umask allows.
            handle = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(document)
            os.replace(scratch, scoped)
        finally:
            scratch.unlink(missing_ok=True)
        return scoped

    def _ambient_target(self) -> ClusterTarget | None:
        """The cluster the sidecar's own kubeconfig points at, if any.

        This is the file `bootstrap` asked gcloud to write, so reading it is not
        the same act as reading one the agent handed over — nothing here is
        caller-controlled. It matters because `KUBECONFIG` is set in the base
        environment: a `kubectl` request that names no kubeconfig at all still
        reaches a cluster, and if the pool did not cover that path it would be
        the one door left open onto the ambient credential.
        """
        try:
            text = Path(self.environment["KUBECONFIG"]).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return None
        context = read_current_context(text)
        return parse_gke_context(context) if context else None

    def _default_kubeconfig(self) -> Path:
        """The scoped stand-in for the environment's `KUBECONFIG`.

        Refuses when the sidecar's own kubeconfig names no GKE cluster. That is
        the fail-closed direction and it is deliberate: the alternative is
        letting the request through on the base environment, which is exactly
        the ambient credential the pool exists to stop handing out.
        """
        target = self._ambient_target()
        if target is None:
            raise scoped_sa_pool.PoolRefusal(
                "the scoped service account pool is armed and this request names no"
                " cluster; the sidecar's own kubeconfig does not identify a GKE"
                " cluster either, so there is no scope to select an account for"
            )
        return self._kubeconfig_for(target)

    def _reroute_kubeconfig_flags(
        self, command: list[str], *, scoped: bool = True
    ) -> tuple[list[str], Path | None]:
        """Point any `--kubeconfig` in argv at the regenerated file.

        kubectl prefers this flag over the environment, and it reaches the broker
        untouched — the policy engine matches on argv but has no rule for it.
        Left alone it would be the simplest way around everything
        `_resolve_kubeconfig` does. The value is a context name by the time it
        gets here: the shim rewrote it from a path in the pod that has the file
        (`credential_proxy_client.resolve_kubeconfig_flags`).

        Returns the rewritten argv and the path the flag ends up naming, or None
        when there was no flag. The caller needs to know: resolving the flag has
        already put its cluster through pool selection, and selecting a *second*
        cluster for the same request is not a second control, it is a bug. The
        last flag wins, the way kubectl reads them.
        """
        rewritten = list(command)
        resolved_path: Path | None = None
        index = 1
        while index < len(rewritten):
            argument = rewritten[index]
            if argument == "--kubeconfig" and index + 1 < len(rewritten):
                resolved_path = self._resolve_kubeconfig(rewritten[index + 1], scoped=scoped)
                rewritten[index + 1] = str(resolved_path)
                index += 2
                continue
            if argument.startswith("--kubeconfig="):
                resolved_path = self._resolve_kubeconfig(
                    argument.split("=", 1)[1], scoped=scoped
                )
                rewritten[index] = f"--kubeconfig={resolved_path}"
            index += 1
        return rewritten, resolved_path

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
        wants_kubeconfig: bool,
    ) -> ExecutionResult:
        """Run the one command that is allowed to author a kubeconfig.

        gcloud writes into the proxy's own directory. The generated file is then
        filed under the context it selects — that read is trustworthy because
        gcloud, not the agent, just wrote it — and returned to the caller so the
        agent's pod can keep the visible pin that `cluster_agent_profile.py`
        records and the Cluster Agent preflight stats. That copy is an artefact
        for the agent to look at; it is never what a later command runs against,
        because a later command names a cluster and this side regenerates the
        file from that name.

        Returned rather than written: the destination is a path in the agent's
        pod, which this process cannot see and must not be handed a route into.
        """
        if not wants_kubeconfig:
            # No destination asked for, so gcloud updates the broker's own
            # config as it always has. Nothing agent-authored is involved.
            return self._execute(command, stdin=stdin, cwd=cwd)

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
                result = replace(result, kubeconfig=generated)
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
        extra_config: tuple[tuple[str, str], ...] = (),
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
                # all of them, a refusal on the content or version-control path
                # reads as though the agent-shared containment fired, which
                # sends whoever is debugging it to the wrong control. Derived
                # from the root rather than from a branch per caller, so a
                # fourth door cannot be added without naming itself here.
                named = {
                    self.workspace_dir: "the shared workspace",
                    self.content_workspace_root: "the content workspace",
                    self.vcs_root: "the version-control scratch tree",
                }.get(root, str(root))
                raise ValueError(f"working directory is outside {named}")
            command_cwd = requested_cwd
        command_environment = self.environment.copy()
        if argv and Path(argv[0]).name == "git":
            command_environment.update(self.git_identity)
        if extra_config:
            # Rebuilt rather than appended to, because the count and the keys
            # have to move together. The caller's pairs go first so the forced
            # set still wins on a key both name -- git takes the last value in
            # the layer, and the pins are what the last position is for.
            command_environment.update(
                _git_forced_config_environment(
                    (
                        *extra_config,
                        ("core.hooksPath", str(self.git_hooks_dir)),
                        *GIT_FORCED_CONFIG,
                    )
                )
            )
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


def build_vcs_broker(executor: CommandExecutor):
    """The version-control broker. Always built; there is no switch.

    Unlike the content workspace this has no off state. It is the forge-neutral
    route, and a build that could return None here would be a build where the
    neutral route is absent and every caller silently falls back to the one
    thing it was meant to replace: a forge CLI, spelled `gh`.

    The scratch tree is the broker's own -- same requirement as content
    passing, same check, and it is a construction-time refusal for the same
    reason: an overlap makes "the agent has no path to it" false while the
    code goes on claiming it.
    """
    from content_workspace import assert_disjoint_roots

    assert_disjoint_roots(
        executor.vcs_root, executor.workspace_dir, purpose="version-control scratch"
    )
    broker = vcs_broker.VcsBroker(
        executor.vcs_root,
        git_runner=executor.execute_vcs_git,
        cli_runner=executor.execute_forge_cli,
        refresh=executor.refresh_forge_credential,
    )
    LOGGER.info(
        "version control enabled root=%s forges=%s",
        executor.vcs_root,
        ",".join(sorted(forge.name for forge in broker.registry.forges)) or "none",
    )
    return broker


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
    # Named `vcs` rather than `vcs_broker`: a class attribute of that name does
    # not shadow the module inside a method, but it reads as though it does.
    # Unlike `workspaces` this is never None on a running broker -- version
    # control is not behind a switch, because it is the only way the sandbox
    # reaches a repository at all.
    vcs: vcs_broker.VcsBroker | None = None
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
        if not self._role_permits(self.principal):
            return None
        return self.principal

    def _role_permits(self, principal: Principal) -> bool:
        """Answer 403 and return False if this caller's side may not use this route.

        Separate from authentication because the answer is a different one: 401
        says "I do not know who you are", 403 says "I do, and this is not
        yours". Collapsing them would tell the gateway its token had expired
        when what happened is that it asked for a route belonging to the shell.

        A principal with no role reaches everything. That is the
        ``NullAuthenticator`` behind a Unix socket, and a broker whose operator
        has not been upgraded to project a second audience yet; ``role`` is set
        only where the API server confirmed which audience it validated.
        """
        needed = required_role(self.path)
        if not needed or not principal.role or principal.role == needed:
            return True
        LOGGER.warning(
            "refused a route this caller's role does not reach path=%s role=%s needed=%s",
            _sanitize_for_logging(self.path),
            principal.role,
            needed,
        )
        self._json(
            HTTPStatus.FORBIDDEN,
            {
                "error": "this route is not available to this caller",
                "code": "CALLER_ROLE_FORBIDDEN",
            },
        )
        return False

    def _repository_is_permitted(self, repository: str) -> bool:
        """Answer 403 and return False unless this install registered ``repository``.

        The broker is where this belongs and where it has not been until now.
        `SOUL.md` tells the agent to check the managed-repository list before
        acting, and the GitOps skills do -- but that is the agent policing
        itself with the list it was handed, which is advice rather than a
        control. Everything downstream of this method spends the installation
        token, so the question "is this a repository we act on" has to be
        answered on the side that holds the credential.

        An unreadable list refuses rather than allows, and says which of the two
        it was in the log: an authorization check that fails open is not one.
        """
        try:
            permitted = repository_is_managed(repository)
        except Exception as exc:
            LOGGER.warning(
                "refusing a repository request: the managed-repository list "
                "could not be read type=%s",
                type(exc).__name__,
            )
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": "the managed repository list is unavailable",
                    "code": "MANAGED_REPOSITORIES_UNAVAILABLE",
                },
            )
            return False
        if permitted:
            return True
        LOGGER.warning(
            "refused a repository this install does not manage repository=%s",
            _sanitize_for_logging(repository),
        )
        self._json(
            HTTPStatus.FORBIDDEN,
            {
                "error": (
                    "this repository is not one the agent manages; register it "
                    "in the gitops-state ConfigMap first"
                ),
                "code": "REPOSITORY_NOT_MANAGED",
            },
        )
        return False

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
        if self.path == "/v1/forge/refresh":
            self._handle_forge_refresh()
            return
        if self.path == "/v1/github/refresh":
            # The name this route had before there was more than one forge.
            # Kept for one release because the caller and the broker are
            # separate images and an upgrade does not move them together --
            # which is also why the provider travels in the body rather than in
            # the path on the route that replaces it.
            self._handle_forge_refresh(provider="github")
            return
        if self.path.startswith("/v1/workspace/"):
            self._handle_workspace_post()
            return
        if self.path.startswith("/v1/vcs/"):
            self._handle_vcs_post()
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
            # A GKE context name, not a path: the file it came from is in the
            # caller's pod. `_resolve_kubeconfig` holds it to the grammar.
            kubeconfig_context = payload.get("kubeconfigContext")
            if kubeconfig_context is not None and not isinstance(kubeconfig_context, str):
                raise ValueError("kubeconfigContext must be a string")
            wants_kubeconfig = bool(payload.get("wantsKubeconfig", False))
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
                argv,
                stdin=stdin,
                cwd=cwd,
                kubeconfig_context=kubeconfig_context,
                wants_kubeconfig=wants_kubeconfig,
            )
        except scoped_sa_pool.PoolRefusal as exc:
            # A refusal, not a fault and not a caller error: the request was
            # well formed and the deployment holds no credential narrow enough
            # to serve it. Answered with its own rule id so that an operator
            # reading the logs sees an unprovisioned cluster rather than a
            # generic policy block, and so that a test can assert on the reason
            # rather than on a status code every other gate also returns.
            LOGGER.warning(
                # The message embeds the scope key, which is built from the
                # `current-context` of a kubeconfig the agent wrote. Same
                # reasoning as the ValueError handler below: an unsanitised
                # value here forges log records.
                "scoped service account refused request_id=%s reason=%s",
                request_id,
                _sanitize_for_logging(str(exc), max_length=256),
            )
            self._json(
                HTTPStatus.FORBIDDEN,
                {
                    "status": "blocked",
                    "code": "SECURITY_POLICY_BLOCKED",
                    "rule": "gcp.scoped-sa.unmapped-scope",
                    "message": str(exc),
                },
            )
            return
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
        response = {
            "status": "completed",
            "exitCode": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "durationMs": result.duration_ms,
            "truncated": result.truncated,
            "timedOut": result.timed_out,
        }
        # Only `get-credentials` fills this, and only when the caller asked for
        # the file. It is gcloud's own output, not anything the agent wrote.
        if result.kubeconfig:
            response["kubeconfig"] = result.kubeconfig
        self._json(HTTPStatus.OK, response)

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
            # A code as well as the status. A caller that can do either
            # content-passing or a working-tree clone has to tell "the broker
            # does not have this armed" from "that verb does not exist", and a
            # bare 404 answers both. See
            # `credential_proxy_client.workspaces_available`.
            self._json(
                HTTPStatus.NOT_FOUND,
                {
                    "status": "not_found",
                    "code": "CONTENT_WORKSPACES_DISABLED",
                    "message": "content workspaces are not enabled on this broker",
                },
            )
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
            requested = payload.get("repo")
            if not is_valid_repository(requested):
                # A ContentWorkspaceError rather than a ValueError, though both
                # answer 400. `credential_proxy_client.workspaces_available`
                # probes this route with an empty repo to find out whether the
                # broker serves it at all, so a malformed slug is a reply this
                # route owes an error *code* for, and the code is what tells a
                # probe apart from a caller that got the name wrong. It also
                # keeps the refusal on the same exception family as the write
                # gate below, so a caller catching one catches both.
                raise content_workspace.ContentWorkspaceError(
                    "repo must be owner/name"
                )
            # No managed-repository gate here. `inspect-repository` exists to
            # read code this install does not manage -- a dependency, an
            # upstream project, a repository named in an issue -- so gating the
            # clone would take the skill away rather than take a capability
            # away. The gate is on `commit` and `push` below, which are where
            # the installation token stops reading and starts writing.
            workspace = store.open(
                requested,
                payload.get("base") or None,
                payload.get("branch") or None,
                payload.get("depth"),
            )
            return {
                "handle": workspace.handle,
                "repo": workspace.repo,
                "base": workspace.base,
                "baseSha": workspace.base_sha,
                "branchSha": workspace.branch_sha,
                "startedFrom": workspace.started_from,
                "shallow": workspace.shallow,
            }
        if route == "read":
            # `paths` is the batched form and answers a different shape. Keyed
            # on its presence rather than on a separate route so that a caller
            # reading one file and a caller reading forty use one verb.
            if payload.get("paths") is not None:
                return store.read_many(payload.get("handle"), payload.get("paths"))
            content = store.read(payload.get("handle"), payload.get("path"))
            return {
                "path": payload.get("path"),
                "contentBase64": base64.b64encode(content).decode("ascii"),
                "size": len(content),
            }
        if route == "list":
            return store.list(
                payload.get("handle"),
                payload.get("prefix") or None,
                payload.get("after") or None,
            )
        if route == "grep":
            return store.grep(
                payload.get("handle"),
                payload.get("pattern"),
                payload.get("prefix") or None,
                regex=payload.get("regex") is True,
                ignore_case=payload.get("ignoreCase") is True,
            )
        if route == "commit":
            require_managed_workspace(store, payload.get("handle"))
            changes = content_workspace.parse_changes(payload.get("changes"))
            return store.commit(
                payload.get("handle"),
                payload.get("branch"),
                payload.get("message"),
                changes,
                expected_base_sha=payload.get("expectedBaseSha") or None,
                expected_branch_sha=payload.get("expectedBranchSha") or None,
            )
        if route == "push":
            require_managed_workspace(store, payload.get("handle"))
            return store.push(payload.get("handle"), payload.get("branch"))
        if route == "close":
            store.close(payload.get("handle"))
            return {"closed": True}
        return None

    def _handle_forge_refresh(self, provider: str = "") -> None:
        """`POST /v1/forge/refresh` — make a credential current before it is spent.

        The provider travels in the body rather than in the path so that the
        route's shape does not change when a second forge arrives, and so a
        caller running an older image than the broker (or the other way round)
        is a rejected value rather than a 404 that reads as "this broker is too
        old".

        The repository is validated by resolving it, which is the same parse the
        broker itself would use: it accepts whatever shape the named forge's
        repositories actually have -- two segments on one forge, a nested
        namespace on another -- and refuses a host this install serves no
        credential for, before that host is told anything.
        """
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > self.max_request_bytes:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            forge, repository = forge_registry().resolve(payload.get("repository"))
            named = provider or payload.get("provider") or forge.name
            if named != forge.name:
                raise ValueError(
                    f"{named} does not serve the repository this request names"
                )
        except providers.WorkspaceError as exc:
            self._json(HTTPStatus(exc.status), _redacted_fields(exc))
            return
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if not self._repository_is_permitted(repository):
            return

        try:
            self.executor.refresh_forge_credential(forge.name, repository)
        except PermissionError:
            # `refresh_forge_credential` asks the managed list too, because the
            # in-process callers do not come through here. Reaching it from this
            # route means the two answers disagreed, which is a race with a
            # ConfigMap remount rather than anything the caller did wrong.
            self._json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "this install does not manage that repository",
                    "code": "REPOSITORY_NOT_MANAGED",
                },
            )
            return
        except Exception as exc:
            # The helper's own stderr is the only place the refusal exists, and
            # `refresh_forge_credential` has already logged it redacted. It must
            # not travel in the response, which crosses back into the sandbox --
            # the reason code is what the caller acts on.
            LOGGER.warning(
                "%s credential refresh failed: %s", forge.name, type(exc).__name__
            )
            self._json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "credential refresh failed",
                    "code": "FORGE_TOKEN_REFRESH_FAILED",
                    "forge": forge.name,
                },
            )
            return
        self._json(HTTPStatus.OK, {"status": "refreshed", "forge": forge.name})

    def _handle_vcs_post(self) -> None:
        """The version-control routes: `POST /v1/vcs/<verb>`.

        A separate namespace rather than more verbs on an existing one, because
        they are a different protocol: every route here stands alone, holds
        nothing across calls and leaves nothing behind, so there is no handle
        argument for any of them to carry.
        """
        if self.vcs is None:
            self._json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "version control is not available on this broker",
                    "code": "VCS_UNAVAILABLE",
                },
            )
            return
        # Hyphens and underscores reach the same route. A caller that guessed
        # the punctuation wrong should not get a 404 that reads as though the
        # verb does not exist.
        verb = self.path[len("/v1/vcs/"):].replace("_", "-")
        route = vcs_broker.route_table(self.vcs).get(verb)
        if route is None:
            self._json(HTTPStatus.NOT_FOUND, {"status": "not_found"})
            return
        try:
            payload = self._read_json_body(
                max_bytes=max(self.max_request_bytes, vcs_broker.max_bundle_bytes() * 2)
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        # The managed-repository control, on the same footing as
        # `require_managed_workspace` on the content routes: the broker holds
        # the forge credential, so "is this a repository we write to" can only
        # be answered here. Nothing downstream answers it -- a forge is handed a
        # repository and spends the token on it -- so this is the whole of the
        # check for these routes.
        #
        # Resolved rather than compared as given, because the managed list holds
        # slugs and a caller may name a repository by URL. Resolving here also
        # rejects a host this install serves no credential for before the write
        # verb is entered, which is the same order `/v1/forge/refresh` uses.
        if verb in vcs_broker.WRITE_VERBS:
            try:
                _, repository = self.vcs.registry.resolve(payload.get("repository"))
            except providers.WorkspaceError as exc:
                self._json(HTTPStatus(exc.status), _redacted_fields(exc))
                return
            if not self._repository_is_permitted(repository):
                return
        try:
            result = route(payload)
        except PermissionError:
            # `BrokeredCredential.ensure` lets this one through, and
            # `refresh_forge_credential` raises it: the credential strategy asks
            # the managed list as well, because the in-process callers do not
            # come through the check above. Reaching it here means the two
            # answers disagreed -- a ConfigMap remount between them -- or that a
            # read verb's forge declined the repository outright.
            self._json(
                HTTPStatus.FORBIDDEN,
                {
                    "error": "this install does not manage that repository",
                    "code": "REPOSITORY_NOT_MANAGED",
                },
            )
            return
        except providers.WorkspaceError as exc:
            self._json(HTTPStatus(exc.status), _redacted_fields(exc))
            return
        except subprocess.CalledProcessError as exc:
            # git's stderr can carry the remote URL with a credential in it, so
            # it goes to the log through the same redactor the exec path uses
            # and never into the response.
            LOGGER.warning(
                "vcs %s failed rc=%s: %s",
                verb,
                exc.returncode,
                redact_credentials(str(exc.stderr or "")[:2000]),
            )
            self._json(
                HTTPStatus.BAD_GATEWAY,
                {"error": f"vcs {verb} failed", "code": "GIT_FAILED"},
            )
            return
        except Exception as exc:
            LOGGER.warning("vcs %s error: %s", verb, type(exc).__name__)
            self._json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "vcs request failed"}
            )
            return
        self._json(HTTPStatus.OK, result)

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
    CredentialProxyHandler.vcs = build_vcs_broker(executor)
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
