"""Say who is calling the inference gateway, from the key the caller sends.

LiteLLM's ``general_settings.custom_auth`` names this module's
``caller_auth``. LiteLLM awaits it for every request to a route that
authenticates (the inference routes, ``/model/info``, ``/health``, the admin
routes), before its own master-key, JWT and database checks, and a
``UserAPIKeyAuth`` it returns is final: no database is read on that path
(LiteLLM v1.108.6, ``litellm/proxy/auth/user_api_key_auth.py``,
``user_api_key_auth_builder``). The probe routes (``/health/liveliness``,
``/health/readiness``) never call it, and ``/metrics`` does not either while
``litellm_settings.require_auth_for_metrics_endpoint`` is false.

What it admits:

- The gateway's master key: the proxy admin, as LiteLLM's own master-key path
  would make it, under the same non-secret alias that path logs.
- A key whose sha256 is in the caller-key map: that caller class, as
  ``key_alias``, ``team_id`` and ``team_alias``, on the inference routes only.
- No key, an empty one, or a placeholder a caller sends today because its
  client insists on some key: the class ``keyless``, on the same routes. The
  gateway took no key until now, so every caller still sends one of these.
- Anything else: refused, 401.

The map is a JSON object from a key's sha256 (lowercase hex) to its class
name, read from a file mounted from a Secret. It holds digests only, never a
key. It is re-read when its modification time, inode or size changes, checked
with one ``stat`` per request, so a Secret update (the kubelet swaps the
mounted file in place) takes effect without a restart. An absent file is an
empty map. A file that does not parse leaves the last map that did in place.

The raw key goes nowhere. LiteLLM writes ``UserAPIKeyAuth.api_key`` into the
Prometheus ``hashed_api_key`` label and the spend metadata, and hashes it only
when it starts with ``sk-`` (``_safe_hash_litellm_api_key``), so a caller's
``api_key`` is the digest, the admin's LiteLLM's master-key alias, and a
keyless caller's ``keyless``. Every refusal is an ``HTTPException`` with
status 401 and a fixed message: LiteLLM's failed-request metric records the
presented key, unhashed when it lacks the ``sk-`` prefix, under any other
status, and logs the exception's text. An unexpected error inside the hook is
turned into the same 401, never re-raised with its own message.

The chart renders this file into the litellm-config ConfigMap and mounts it
beside config.yaml, where LiteLLM resolves the module; the kustomize base
carries a copy (k8s-operator/config/integrations/litellm/base), and
tests/test_litellm_caller_auth.py holds the two identical.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import sys
import threading
from typing import Any

from fastapi import HTTPException
from litellm.constants import LITELLM_PROXY_ADMIN_NAME, LITELLM_PROXY_MASTER_KEY_ALIAS
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth

logger = logging.getLogger("kube_agents.litellm_caller_auth")

# Where the caller-key map is, and the variable that can move it.
CALLER_KEYS_FILE_ENV = "KUBE_AGENTS_CALLER_KEYS_FILE"
DEFAULT_CALLER_KEYS_FILE = "/etc/litellm/caller-keys/keys.json"
# The variable general_settings.master_key reads (os.environ/LITELLM_MASTER_KEY).
MASTER_KEY_ENV = "LITELLM_MASTER_KEY"
# The class a caller with no key, or a placeholder, is admitted as. Also its
# api_key: a fixed word, so the label and the rate-limit bucket are one.
KEYLESS_CLASS = "keyless"
# What callers send today because their client wants a non-empty key: the
# agents' and Hindsight's "none" (deploy/shared/defaults/config.yaml,
# platformagent_manifests.go, a2a/memory/llm.go) and the worker adapter's
# "a2a-playground" (a2a/cmd/worker-adapter/main.go).
PLACEHOLDER_KEYS = frozenset({"none", "a2a-playground"})
# The routes a non-admin caller may use, matched exactly. LiteLLM's own
# allowed_routes check also admits every path under an entry, which would
# take in /health/test_connection and /health/drain beside /health.
CALLER_ROUTES = (
    "/v1/chat/completions",
    "/chat/completions",
    "/v1/messages",
    "/v1/messages/count_tokens",
    "/v1/models",
    "/models",
    "/model/info",
    "/v1/model/info",
    "/health",
)
# A map entry's shape: a sha256 in lowercase hex, and a class name that can
# stand as a Prometheus label value and a Kubernetes name.
KEY_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CLASS_NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# Class names the map may not hand out: the keyless class, so a key cannot
# pass for no key, and the admin's alias.
RESERVED_CLASSES = frozenset({KEYLESS_CLASS, LITELLM_PROXY_MASTER_KEY_ALIAS})
# What every refused caller is told. Fixed text: LiteLLM logs it.
REFUSED_STATUS = 401
REFUSED_KEY_DETAIL = "Authentication Error: this gateway does not recognise the key."
REFUSED_ROUTE_DETAIL = "Authentication Error: this route is not open to this caller."
# The proxy leaves the root logger at WARNING and this logger has no handler
# of its own, so without these a map reload never reaches the pod log.
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"


class _CallerKeyMap:
    """The digest-to-class map, reloaded when the file under it changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stamp: tuple[int, int, int] | None = None
        self._classes: dict[str, str] = {}

    def lookup(self, path: str, key_digest: str) -> str | None:
        """The class for a key digest, after reloading the map if it changed."""
        self._refresh(path)
        return self._classes.get(key_digest)

    def _refresh(self, path: str) -> None:
        try:
            info = os.stat(path)
        except FileNotFoundError:
            stamp = None
        except OSError as err:
            logger.error("caller-key map %s is unreadable (%s); keeping the last map read", path, err.strerror)
            return
        else:
            stamp = (info.st_mtime_ns, info.st_ino, info.st_size)
        if stamp == self._stamp:
            return
        with self._lock:
            if stamp == self._stamp:
                return
            if stamp is None:
                self._classes = {}
                self._stamp = None
                logger.info("caller-key map %s is absent; no caller key is known", path)
                return
            classes = _read_map(path)
            if classes is None:
                # Recorded as seen, so a broken file is reported once, not per request.
                self._stamp = stamp
                return
            self._classes = classes
            self._stamp = stamp
            logger.info("caller-key map %s loaded: %d key(s), %d class(es)", path, len(classes), len(set(classes.values())))


def _read_map(path: str) -> dict[str, str] | None:
    """The map in the file, its bad entries dropped, or None if it does not parse."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as err:
        logger.error("caller-key map %s does not parse (%s); keeping the last map read", path, type(err).__name__)
        return None
    if not isinstance(raw, dict):
        logger.error("caller-key map %s is not a JSON object; keeping the last map read", path)
        return None
    classes: dict[str, str] = {}
    dropped = 0
    for key_digest, caller_class in raw.items():
        if (
            isinstance(key_digest, str)
            and KEY_DIGEST_PATTERN.match(key_digest)
            and isinstance(caller_class, str)
            and CLASS_NAME_PATTERN.match(caller_class)
            and caller_class not in RESERVED_CLASSES
        ):
            classes[key_digest] = caller_class
        else:
            dropped += 1
    if dropped:
        # The count only: an entry that is not a digest could be a pasted key.
        logger.warning("caller-key map %s: dropped %d entry(ies) that are not a sha256 and a class name", path, dropped)
    return classes


_caller_keys = _CallerKeyMap()


def _configure_logging() -> None:
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False


def _request_route(request: Any) -> str:
    """The route LiteLLM authorises against: the ASGI path, root_path stripped."""
    try:
        from litellm.proxy.auth.auth_utils import get_request_route

        return get_request_route(request=request)
    except ImportError:
        scope = getattr(request, "scope", None) or {}
        return str(scope.get("path", ""))


def _caller(caller_class: str, api_key_label: str) -> UserAPIKeyAuth:
    return UserAPIKeyAuth(
        api_key=api_key_label,
        key_alias=caller_class,
        team_id=caller_class,
        team_alias=caller_class,
        user_role=LitellmUserRoles.INTERNAL_USER,
        allowed_routes=list(CALLER_ROUTES),
    )


def _admin() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(
        api_key=LITELLM_PROXY_MASTER_KEY_ALIAS,
        user_id=LITELLM_PROXY_ADMIN_NAME,
        user_role=LitellmUserRoles.PROXY_ADMIN,
    )


def _authenticate(request: Any, api_key: str | None) -> UserAPIKeyAuth:
    # Named so CodeQL's credential heuristic (api...key) watches it.
    master_api_key = os.environ.get(MASTER_KEY_ENV, "")
    if api_key and master_api_key and secrets.compare_digest(api_key.encode(), master_api_key.encode()):
        return _admin()
    if not api_key or api_key in PLACEHOLDER_KEYS:
        token = _caller(KEYLESS_CLASS, KEYLESS_CLASS)
    else:
        key_digest = hashlib.sha256(api_key.encode()).hexdigest()
        caller_class = _caller_keys.lookup(os.environ.get(CALLER_KEYS_FILE_ENV, DEFAULT_CALLER_KEYS_FILE), key_digest)
        if caller_class is None:
            raise HTTPException(status_code=REFUSED_STATUS, detail=REFUSED_KEY_DETAIL)
        token = _caller(caller_class, key_digest)
    if _request_route(request) not in CALLER_ROUTES:
        raise HTTPException(status_code=REFUSED_STATUS, detail=REFUSED_ROUTE_DETAIL)
    return token


async def caller_auth(request: Any, api_key: str | None) -> UserAPIKeyAuth:
    """LiteLLM's custom_auth entry point. Positional: the enterprise wrapper calls it so."""
    try:
        return _authenticate(request, api_key)
    except HTTPException:
        raise
    except Exception as err:
        # The type only: a message could carry the key.
        logger.error("caller auth failed unexpectedly (%s); refusing the request", type(err).__name__)
        raise HTTPException(status_code=REFUSED_STATUS, detail=REFUSED_KEY_DETAIL) from None


_configure_logging()
