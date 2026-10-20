"""Exchange OAuth2 client credentials for the bearer an openai_compatible gateway wants.

An enterprise AI gateway that speaks the OpenAI chat-completions API often
takes no static key: a client id and secret are exchanged at a token URL for a
short-lived bearer token (OAuth2 client_credentials, RFC 6749 section 4.4)
that has to be renewed before it expires. LiteLLM's hosted_vllm handler, which
the chart uses for openai_compatible, sends whatever api_key the request
carries, so this pre-call hook fetches the token, caches it, renews it ahead
of expiry and sets it as the request's api_key on every request whose alias
routes to hosted_vllm deployments. Any other alias is left as sent.

The environment configures it, filled from the Secret litellm.envFromSecret
names:

  OPENAI_COMPATIBLE_OAUTH_TOKEN_URL     the token endpoint; unset, the hook is idle
  OPENAI_COMPATIBLE_OAUTH_CLIENT_ID
  OPENAI_COMPATIBLE_OAUTH_CLIENT_SECRET
  OPENAI_COMPATIBLE_OAUTH_SCOPE         optional, sent as scope
  OPENAI_COMPATIBLE_OAUTH_CLIENT_AUTH   basic (default): id and secret in an
                                        Authorization header; post: in the form

The token request is a form POST through urllib in a worker thread, so the
proxy's event loop is not held. It leaves the pod under the gateway's egress
rules, which open the server's address and port and nothing else private, so
the token URL has to be reachable where the server is: a public address on
443, an address inside litellm.openaiCompatible.egressCIDRs on the apiBase
URL's port, or the server's own namespace on its targetPort. It is verified
against the system roots plus the server's own CA bundle when the alias's
deployments carry an ssl_verify path (litellm.openaiCompatible.caBundleSecretRef),
so a gateway on a private CA can still use a public identity provider. A
response without expires_in is kept for
DEFAULT_TOKEN_LIFETIME_SECONDS. A failed exchange raises, so the request that
needed the token fails with that error rather than leaving without a
credential, and the next request tries again. A token endpoint that redirects
is refused with the redirect's status: the request is a credential-bearing
POST and must not be replayed elsewhere. A server that answers 401 to a
cached token drops it, so the next request fetches a new one instead of
waiting out the stated lifetime.

The chart renders this file into the litellm-config ConfigMap only for
openai_compatible with litellm.envFromSecret set;
tests/test_litellm_oauth2_callback.py holds it to this contract.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

TOKEN_URL_ENV = "OPENAI_COMPATIBLE_OAUTH_TOKEN_URL"
CLIENT_ID_ENV = "OPENAI_COMPATIBLE_OAUTH_CLIENT_ID"
# Named for what it holds, the variable's name, not the credential: CodeQL's
# clear-text queries read "secret" in an identifier as the credential itself.
CLIENT_CREDENTIAL_ENV = "OPENAI_COMPATIBLE_OAUTH_CLIENT_SECRET"
SCOPE_ENV = "OPENAI_COMPATIBLE_OAUTH_SCOPE"
CLIENT_AUTH_ENV = "OPENAI_COMPATIBLE_OAUTH_CLIENT_AUTH"
CLIENT_AUTH_BASIC = "basic"
CLIENT_AUTH_POST = "post"
GRANT_TYPE = "client_credentials"
# Renew this long before the stated expiry, so a request that starts just
# before the deadline does not reach the server with a token it has already
# stopped accepting.
REFRESH_MARGIN_SECONDS = 60
# For a token response that names no expires_in (RFC 6749 recommends it and
# does not require it): short, so a server that rotates quietly is not outrun.
DEFAULT_TOKEN_LIFETIME_SECONDS = 300
TOKEN_REQUEST_TIMEOUT_SECONDS = 15
ACCESS_TOKEN_KEY = "access_token"
EXPIRES_IN_KEY = "expires_in"
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
JSON_CONTENT_TYPE = "application/json"
# The provider prefix the chart gives every openai_compatible deployment.
HOSTED_VLLM_PREFIX = "hosted_vllm/"
MODEL_KEY = "model"
# The request field LiteLLM's router hands through to the provider handler,
# which hosted_vllm sends as the bearer.
REQUEST_CREDENTIAL_KEY = "api_key"
DEPLOYMENT_PARAMS_KEY = "litellm_params"
DEPLOYMENT_MODEL_KEY = "model"
# The per-deployment CA bundle path the chart sets for a server behind a
# private CA; a boolean here is LiteLLM's own on/off and not a path.
DEPLOYMENT_SSL_VERIFY_KEY = "ssl_verify"
# The proxy leaves the root logger at WARNING and this logger has no handler
# of its own; the fetch and renew lines are the sign the hook is at work.
LOG_LEVEL = logging.INFO
LOG_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"

logger = logging.getLogger("litellm_oauth2_callback")

# Whether the hook has already said it cannot see the router, so a LiteLLM
# change that blinds it is logged once rather than on every request.
_blind_warned = False


# The status a server answers with for a token it no longer honours.
UNAUTHORIZED_STATUS = 401


class TokenCache:
    """One bearer token and when it stops being safe to send."""

    def __init__(self) -> None:
        self.token = ""
        self.expires_at = 0.0
        self.lock = asyncio.Lock()

    def usable(self) -> bool:
        return bool(self.token) and time.monotonic() < self.expires_at - REFRESH_MARGIN_SECONDS

    def drop(self) -> None:
        self.token, self.expires_at = "", 0.0


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """urllib would turn the POST into a GET and carry the Basic header to the new host; refuse instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_cache = TokenCache()


def settings_from_env(environ: Any = None) -> dict[str, str] | None:
    """The hook's configuration, or None while the token URL is unset."""
    env = os.environ if environ is None else environ
    token_url = str(env.get(TOKEN_URL_ENV, "")).strip()
    if not token_url:
        return None
    client_id = str(env.get(CLIENT_ID_ENV, "")).strip()
    client_secret = str(env.get(CLIENT_CREDENTIAL_ENV, "")).strip()
    if not client_id or not client_secret:
        raise ValueError(f"oauth2: {TOKEN_URL_ENV} is set but {CLIENT_ID_ENV} or {CLIENT_CREDENTIAL_ENV} is empty")
    client_auth = str(env.get(CLIENT_AUTH_ENV, CLIENT_AUTH_BASIC)).strip().lower() or CLIENT_AUTH_BASIC
    if client_auth not in (CLIENT_AUTH_BASIC, CLIENT_AUTH_POST):
        raise ValueError(f"oauth2: {CLIENT_AUTH_ENV} must be {CLIENT_AUTH_BASIC} or {CLIENT_AUTH_POST}, not {client_auth!r}")
    return {
        "token_url": token_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": str(env.get(SCOPE_ENV, "")).strip(),
        "client_auth": client_auth,
    }


def fetch_token(settings: dict[str, str], ca_bundle: str = "") -> tuple[str, float]:
    """Blocking: run the client_credentials grant; return the token and its lifetime in seconds.

    ``ca_bundle`` is a PEM file trusted for the token endpoint beside the
    system roots: the server's own bundle, so a token endpoint on the same
    private CA verifies, and one on a public CA still does.
    """
    form = {"grant_type": GRANT_TYPE}
    if settings["scope"]:
        form["scope"] = settings["scope"]
    headers = {"Content-Type": FORM_CONTENT_TYPE, "Accept": JSON_CONTENT_TYPE}
    if settings["client_auth"] == CLIENT_AUTH_POST:
        form["client_id"] = settings["client_id"]
        form["client_secret"] = settings["client_secret"]
    else:
        pair = f"{settings['client_id']}:{settings['client_secret']}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
    request = urllib.request.Request(
        settings["token_url"], data=urllib.parse.urlencode(form).encode(), headers=headers, method="POST"
    )
    try:
        # The default context loads the system roots; the bundle is added to
        # them rather than given as cafile, which would replace them. A
        # missing or unreadable bundle fails here, before anything is sent.
        context = None
        if ca_bundle:
            context = ssl.create_default_context()
            context.load_verify_locations(cafile=ca_bundle)
        opener = urllib.request.build_opener(_RefuseRedirects(), urllib.request.HTTPSHandler(context=context))
        with opener.open(request, timeout=TOKEN_REQUEST_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"oauth2: the token endpoint answered {exc.code} {exc.reason}") from None
    except (OSError, TimeoutError, ValueError) as exc:
        # URLError is an OSError: a refused connection, a failed name lookup
        # and a certificate the bundle does not sign all arrive here.
        raise RuntimeError(f"oauth2: the token request failed: {exc}") from None
    token = body.get(ACCESS_TOKEN_KEY) if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"oauth2: the token response carries no {ACCESS_TOKEN_KEY}")
    try:
        lifetime = float(body.get(EXPIRES_IN_KEY, DEFAULT_TOKEN_LIFETIME_SECONDS))
    except (TypeError, ValueError):
        lifetime = float(DEFAULT_TOKEN_LIFETIME_SECONDS)
    # json accepts NaN and Infinity, and float() accepts "inf": a lifetime that
    # is not a finite positive number would pin the token forever or never, so
    # it is treated as absent.
    if not math.isfinite(lifetime) or lifetime <= 0:
        lifetime = float(DEFAULT_TOKEN_LIFETIME_SECONDS)
    return token, lifetime


async def bearer_for(settings: dict[str, str], cache: TokenCache, ca_bundle: str = "") -> str:
    """The cached token while it is safe to send, otherwise a fresh one; one fetch at a time."""
    if cache.usable():
        return cache.token
    async with cache.lock:
        if cache.usable():
            return cache.token
        verb = "renewed" if cache.token else "fetched"
        try:
            token, lifetime = await asyncio.to_thread(fetch_token, settings, ca_bundle)
        except Exception as exc:  # noqa: BLE001 - the request that needed the token gets the error
            # The exception text already starts with the oauth2 prefix.
            logger.warning("%s; the request fails and the next one tries again", exc)
            raise
        cache.token, cache.expires_at = token, time.monotonic() + lifetime
        logger.info("oauth2: %s a bearer token from %s, valid for %d s", verb, settings["token_url"], int(lifetime))
        if lifetime <= REFRESH_MARGIN_SECONDS:
            logger.warning("oauth2: the token lives %d s, within the %d s renewal margin, so every request renews it", int(lifetime), REFRESH_MARGIN_SECONDS)
        return token


def hosted_vllm_deployments(model_name: str, router: Any) -> list[dict]:
    """The litellm_params of every deployment ``router`` serves ``model_name`` with, when all are hosted_vllm.

    Empty when the alias cannot be resolved or any deployment is another
    provider's: the request then goes out as it was, and the hook says once
    when it is blind.
    """
    if router is None:
        _warn_blind("no LiteLLM router to resolve aliases with")
        return []
    if not model_name:
        return []
    try:
        deployments = router.get_model_list(model_name=model_name) or []
    except Exception as exc:  # noqa: BLE001 - an unresolvable alias is left alone
        _warn_blind(f"the router could not resolve {model_name!r} ({exc})")
        return []
    params = [d.get(DEPLOYMENT_PARAMS_KEY) or {} for d in deployments]
    models = [p.get(DEPLOYMENT_MODEL_KEY, "") for p in params]
    # No deployments is what the router answers for an alias it does not
    # serve, which the proxy refuses right after this hook: not this hook's
    # concern and not a sign it is blind, so no warning.
    if not models or not any(models) or not all(str(m).startswith(HOSTED_VLLM_PREFIX) for m in models):
        return []
    return params


def ca_bundle_of(deployments: list[dict]) -> str:
    """The ssl_verify path the deployments share, or "" for the system roots."""
    paths = {p.get(DEPLOYMENT_SSL_VERIFY_KEY) for p in deployments}
    paths = {p for p in paths if isinstance(p, str) and p}
    return paths.pop() if len(paths) == 1 else ""


def _warn_blind(reason: str) -> None:
    global _blind_warned
    if not _blind_warned:
        _blind_warned = True
        logger.warning("oauth2: hook inactive, %s; requests go out without a bearer token", reason)


def _configure_logger() -> None:
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False


def _router() -> Any:
    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:  # noqa: BLE001 - outside the proxy there is no router
        return None
    return llm_router


class OAuth2ClientCredentials(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: dict, call_type: Any) -> dict:
        settings = settings_from_env()
        if settings is None:
            return data
        deployments = hosted_vllm_deployments(data.get(MODEL_KEY, ""), _router())
        if not deployments:
            return data
        data[REQUEST_CREDENTIAL_KEY] = await bearer_for(settings, _cache, ca_bundle_of(deployments))
        return data

    async def async_post_call_failure_hook(
        self, request_data: dict, original_exception: Exception, user_api_key_dict: Any, traceback_str: Any = None
    ) -> None:
        # The server no longer honours the token it was sent (revoked, a
        # rotated signing key, a restarted token store): forget it, so the next
        # request exchanges for a new one instead of waiting out the stated
        # lifetime. Only for an alias this hook supplies the token to, and
        # only when the refused token is the one still cached: a late 401 for
        # a token already replaced must not drop its successor.
        request_data = request_data or {}
        if getattr(original_exception, "status_code", None) != UNAUTHORIZED_STATUS or not _cache.token:
            return None
        if request_data.get(REQUEST_CREDENTIAL_KEY) != _cache.token:
            return None
        if not hosted_vllm_deployments(request_data.get(MODEL_KEY, ""), _router()):
            return None
        _cache.drop()
        logger.warning("oauth2: the server answered %d to the cached bearer token; dropped it, the next request fetches a new one", UNAUTHORIZED_STATUS)
        return None


_configure_logger()
proxy_handler_instance = OAuth2ClientCredentials()
