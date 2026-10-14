"""github_api.py — shared GitHub API client for repository maintenance scripts.

Combines bounded retries on transient errors (5xx, 429, secondary rate limit 403)
with automatic list endpoint pagination (`get_all`), so callers neither drop
writes on transient blips nor silently truncate multi-page lists. `graphql()`
goes through the same retries and raises on an `errors` body, so a partial
answer is never mistaken for a whole one. `workflows()` pages the one list
endpoint that wraps its page in an object, and `is_refusal()` is the 4xx test
the scheduled sweeps share for "ours to fix" against "wait for the next one".
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
GRAPHQL_PATH = "/graphql"
DEFAULT_USER_AGENT = "kube-agents-scripts"

REQUEST_ATTEMPTS = 3
REQUEST_RETRY_SECONDS = 5
REQUEST_RETRY_CEILING = 60
PER_PAGE = 100
# The status that separates GitHub refusing a request from GitHub failing.
SERVER_ERROR_FLOOR = 500
TOO_MANY_REQUESTS = 429


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


def is_refusal(error) -> bool:
    """A 4xx other than a rate limit: a permission or a request GitHub rejects,
    which retrying will not change and a person has to fix. A scheduled sweep
    reds on one of these and leaves anything else to its next run."""
    return (
        isinstance(error, urllib.error.HTTPError)
        and error.code < SERVER_ERROR_FLOOR
        and error.code != TOO_MANY_REQUESTS
        and not _rate_limited(error)
    )


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

    def workflows(self):
        """Every workflow the repository has, paged to the endpoint's own total.

        The list endpoint returns `{"total_count": n, "workflows": [...]}`, so
        `get_all`, which pages a bare list, cannot read it. An empty page ends
        the loop as well as reaching the total, so a total that overstates the
        list cannot spin this.
        """
        found = []
        page = 1
        while True:
            query = urllib.parse.urlencode({"per_page": PER_PAGE, "page": page})
            batch = self.get(f"/repos/{self.repo}/actions/workflows?{query}")
            found.extend(batch["workflows"])
            if not batch["workflows"] or len(found) >= batch["total_count"]:
                return found
            page += 1

    def post(self, path: str, payload=None, tolerate=()):
        return self.request("POST", path, payload, tolerate=tolerate)

    def patch(self, path: str, payload=None, tolerate=()):
        return self.request("PATCH", path, payload, tolerate=tolerate)

    def delete(self, path: str, tolerate=()):
        return self.request("DELETE", path, tolerate=tolerate)

    def graphql(self, query: str, variables=None):
        """One GraphQL call. Errors in the body are raised, not returned.

        GraphQL answers 200 to a query that failed and puts the failure under
        `errors`, so a caller reading `data` alone would take a partial answer
        for a whole one.
        """
        result = self.post(GRAPHQL_PATH, {"query": query, "variables": variables or {}})
        errors = (result or {}).get("errors")
        if errors:
            raise RuntimeError(f"GraphQL: {errors}")
        return result["data"]
