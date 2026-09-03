"""github_api.py — shared GitHub API client for repository maintenance scripts.

Combines bounded retries on transient errors (5xx, 429, secondary rate limit 403)
with automatic list endpoint pagination (`get_all`), so callers neither drop
writes on transient blips nor silently truncate multi-page lists.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
DEFAULT_USER_AGENT = "kube-agents-scripts"

REQUEST_ATTEMPTS = 3
REQUEST_RETRY_SECONDS = 5
REQUEST_RETRY_CEILING = 60
PER_PAGE = 100


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _rate_limited(error: urllib.error.HTTPError) -> bool:
    """Whether a 403 is GitHub throttling rather than refusing.

    A secondary rate limit comes back as 403, not 429, and is the one 4xx worth
    retrying on a write path. It carries `Retry-After`, or an exhausted primary
    quota; a permissions 403 carries neither, so this does not turn a missing
    permission into fifteen seconds of retries.
    """
    headers = getattr(error, "headers", None) or {}
    return headers.get("Retry-After") is not None or headers.get("x-ratelimit-remaining") == "0"


def _retry_delay(error: urllib.error.HTTPError) -> int:
    """`Retry-After` when GitHub names one, capped, else the fixed backoff."""
    headers = getattr(error, "headers", None) or {}
    requested = headers.get("Retry-After")
    if requested and str(requested).strip().isdigit():
        return min(int(str(requested).strip()), REQUEST_RETRY_CEILING)
    return REQUEST_RETRY_SECONDS


class GitHubAPI:
    def __init__(
        self,
        repo: str,
        token: str,
        root: str = API_ROOT,
        user_agent: str = DEFAULT_USER_AGENT,
        opener=urllib.request.urlopen,
        sleep=time.sleep,
    ):
        self.repo = repo
        self.token = token
        self.root = root
        self.user_agent = user_agent
        self.opener = opener
        self.sleep = sleep

    def request(self, method: str, path: str, payload=None, tolerate=()):
        """One API call, retried on the failures that are worth retrying.

        A dropped write is the failure this whole client exists to prevent — so
        a 5xx, a 429, and the 403 form of a secondary rate limit each get three
        attempts, honouring `Retry-After` when GitHub sends one. A 403 that is
        really a missing permission is not retried. `tolerate` names status
        codes the caller has a meaning for; they come back as None instead of
        raising.
        """
        body = None if payload is None else json.dumps(payload).encode()
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            request = urllib.request.Request(
                f"{self.root}{path}",
                method=method,
                data=body,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "User-Agent": self.user_agent,
                    "X-GitHub-Api-Version": API_VERSION,
                },
            )
            try:
                with self.opener(request) as response:
                    raw = response.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                if error.code in tolerate:
                    return None
                retryable = (
                    error.code == 429
                    or 500 <= error.code < 600
                    or (error.code == 403 and _rate_limited(error))
                )
                if not retryable or attempt == REQUEST_ATTEMPTS:
                    raise
                log(f"{method} {path} returned {error.code}; retrying ({attempt}/{REQUEST_ATTEMPTS})")
                delay = _retry_delay(error)
            except urllib.error.URLError as error:
                if attempt == REQUEST_ATTEMPTS:
                    raise
                log(f"{method} {path} unreachable ({error.reason}); retrying ({attempt}/{REQUEST_ATTEMPTS})")
                delay = REQUEST_RETRY_SECONDS
            self.sleep(delay)

    def get(self, path: str, tolerate=()):
        return self.request("GET", path, tolerate=tolerate)

    def get_all(self, path: str, per_page: int = PER_PAGE):
        """Every page of a list endpoint. A truncated list reads as complete."""
        separator = "&" if "?" in path else "?"
        items = []
        page = 1
        while True:
            batch = self.request("GET", f"{path}{separator}per_page={per_page}&page={page}")
            if not isinstance(batch, list):
                return items
            items.extend(batch)
            if len(batch) < per_page:
                return items
            page += 1

    def post(self, path: str, payload=None, tolerate=()):
        return self.request("POST", path, payload, tolerate=tolerate)

    def patch(self, path: str, payload=None, tolerate=()):
        return self.request("PATCH", path, payload, tolerate=tolerate)

    def delete(self, path: str, tolerate=()):
        return self.request("DELETE", path, tolerate=tolerate)
