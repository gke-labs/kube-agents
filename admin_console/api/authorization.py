"""Process-scoped authorization for the local admin portal API."""

from __future__ import annotations

import os
import secrets
from functools import cache

PORTAL_API_TOKEN_ENV = "KUBE_AGENTS_PORTAL_API_TOKEN"
MINIMUM_TOKEN_LENGTH = 32


@cache
def portal_api_token() -> str:
    """Return one token shared by the portal and its Streamlit child process."""

    token = os.environ.get(PORTAL_API_TOKEN_ENV, "").strip()
    if token and len(token) < MINIMUM_TOKEN_LENGTH:
        raise RuntimeError(
            f"{PORTAL_API_TOKEN_ENV} must contain at least "
            f"{MINIMUM_TOKEN_LENGTH} characters"
        )
    if not token:
        token = secrets.token_urlsafe(32)
        os.environ[PORTAL_API_TOKEN_ENV] = token
    return token


def portal_api_headers() -> dict[str, str]:
    """Build the authorization header for a portal-owned API client."""

    return {"Authorization": f"Bearer {portal_api_token()}"}
