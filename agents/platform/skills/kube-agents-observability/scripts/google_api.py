#!/usr/bin/env python3
"""The one way the observability helpers reach a Google API: the credential broker.

The sandbox holds no Google credential. `gcloud` there is a shim to the broker,
whose allowlist carries no command that prints an access token, so a helper
that fetches a token by hand exits 1 before it reads anything -- and an agent left
without a working path hand-rolls a bearer header that lands in its transcript.
These helpers open `credential_proxy_client.ApiSession` instead: a GET to the
real Google URL is rewritten onto the broker's read-only relay, checked against
`api_policy.API_READ_ROUTES`, and forwarded on the broker's own identity. No
Google token exists in this process, so none can be printed or pasted.
"""

from __future__ import annotations

import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The shared client, in the pod (`/opt/defaults/scripts`, the operator's copy;
# `/opt/data/scripts`, the profile's) and in a source checkout, where nothing
# is staged into /opt. The same three entries api_deprecation_scan.py uses.
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

import credential_proxy_client  # noqa: E402

# The longest silence a helper waits through for bytes on one relayed read,
# per wait, not for the whole read. The broker buffers the upstream's body
# before it writes a status line, and answers 504 (UPSTREAM_TIMEOUT) when the
# upstream passes its own API_RELAY_DEADLINE_S, 120 s in credential_proxy.py.
# This value sits above that deadline so a slow Monitoring or Trace page is
# reported as the broker's 504 naming the upstream, and the helper's own
# timeout means the broker itself went quiet past the deadline it enforces.
# The test beside the helpers holds the ordering against the broker's
# constant.
RELAY_TIMEOUT_SECONDS = 150
# The connect is bounded separately, by the client's own figure: `requests`
# takes a (connect, read) pair, and a scalar would make a SYN nobody answers
# (a default-deny egress policy drops rather than refuses) a 150 s silence
# where the shim gives up after BROKER_CONNECT_TIMEOUT_SECONDS.
RELAY_TIMEOUT = (credential_proxy_client.BROKER_CONNECT_TIMEOUT_SECONDS, RELAY_TIMEOUT_SECONDS)

# The keys of the broker's refusal body (`/v1/gcp` answers 403 the way
# `/v1/exec` does, naming the api_policy rule) and of a Google API error body.
BROKER_RULE_KEY = "rule"
BROKER_MESSAGE_KEY = "message"
GOOGLE_ERROR_KEY = "error"

# The 2xx range, and how much of an unstructured error body to show.
HTTP_OK_FIRST = 200
HTTP_OK_LAST = 299
ERROR_BODY_PREVIEW_CHARS = 500

# What a helper exits with when the read did not happen.
EXIT_READ_FAILED = 1

# The Cloud Trace v1 reads the relay carries (api_policy.API_READ_ROUTES),
# written once here: analyze_trace_latency.py and fetch_traces.py both list,
# and only the first then reads each trace.
TRACE_LIST_URL = "https://cloudtrace.googleapis.com/v1/projects/{project}/traces"
TRACE_GET_URL = "https://cloudtrace.googleapis.com/v1/projects/{project}/traces/{trace_id}"

# How the helpers spell a window boundary to Google, and the window they
# read by default.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_WINDOW_HOURS = 24

# How every helper prints its JSON.
JSON_INDENT = 2

# List reads page: the caller's `pageToken` and the reply's `nextPageToken`.
# Cloud Trace's list answers an empty first page with a token when the newest
# time bucket holds nothing (observed on a live install), so a reader that
# stops at the first page reports "no traces" over a project full of them;
# Monitoring's descriptor list is too large to relay in one page. The cap
# bounds a list that never runs dry, and reaching it is reported, never
# passed off as the end of the list: with nothing in hand the read fails
# (an empty answer would read as an empty window), with something in hand the
# helper's stdout is complete for what it says and stderr says it is partial.
PAGE_TOKEN_PARAM = "pageToken"
NEXT_PAGE_TOKEN_KEY = "nextPageToken"
MAX_LIST_PAGES = 50
PAGE_CAP_NOTE = "stopped after {pages} pages with more to read"
# The smallest limit a list read takes. Cloud Trace reads a pageSize of 0 or
# less as "choose a default" and answers a full page, which a stop rule of
# `len(items) >= limit` would then trim to nothing, so the analyzer would say
# "No traces found" over a project that just answered with traces.
MIN_LIMIT = 1


class RelayError(Exception):
    """A relayed read that did not return 2xx; the message is what the helper prints."""


def open_session():
    """The requests-shaped session every helper reads through."""
    try:
        return credential_proxy_client.ApiSession()
    except RuntimeError as exc:
        raise RelayError(str(exc)) from exc


def describe_failure(url: str, response) -> str:
    """One line saying why the read failed, with the broker's rule when it refused."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and BROKER_RULE_KEY in body:
        return (
            f"the credential broker refused {url} ({body[BROKER_RULE_KEY]}): "
            f"{body.get(BROKER_MESSAGE_KEY, '')}"
        )
    if isinstance(body, dict) and isinstance(body.get(GOOGLE_ERROR_KEY), dict):
        error = body[GOOGLE_ERROR_KEY]
        return (
            f"HTTP {response.status_code} from {url}: "
            f"{error.get('status', '')} {error.get('message', '')}".strip()
        )
    text = response.text if isinstance(response.text, str) else ""
    return f"HTTP {response.status_code} from {url}: {text[:ERROR_BODY_PREVIEW_CHARS]}"


def timeout_exceptions() -> tuple[type[BaseException], ...]:
    """What a timed-out read raises: the stdlib's, and `requests`' where the session is one.

    `requests.exceptions.Timeout` is an `OSError` (`RequestException` subclasses
    `IOError`), so without this it reads as the broker being unreachable, and
    a broker that was reached and is waiting on Google is reported as down.
    `requests` is imported here and not at module scope, as the client does:
    it is in the sandbox image and a test's injected session need not be one.
    """
    try:
        import requests
    except ImportError:
        return (TimeoutError,)
    return (TimeoutError, requests.exceptions.Timeout)


def connect_timeout_exceptions() -> tuple[type[BaseException], ...]:
    """What a connect nobody answered raises; a `Timeout` too, so it is told apart first."""
    try:
        import requests
    except ImportError:
        return ()
    return (requests.exceptions.ConnectTimeout,)


def get_json(session, url: str, *, params=None) -> dict:
    """One relayed GET, decoded; raises RelayError with the reason on anything else."""
    try:
        response = session.get(url, params=params, timeout=RELAY_TIMEOUT)
    except credential_proxy_client.TokenUnavailable as exc:
        raise RelayError(f"no caller token for the credential broker: {exc}") from exc
    except connect_timeout_exceptions() as exc:
        raise RelayError(
            f"could not reach the credential broker for {url}: no connection within "
            f"{credential_proxy_client.BROKER_CONNECT_TIMEOUT_SECONDS:g}s, so the path to it is "
            f"closed or the broker is not listening: {exc}"
        ) from exc
    except timeout_exceptions() as exc:
        raise RelayError(
            f"the credential broker did not answer within {RELAY_TIMEOUT_SECONDS}s for {url}, "
            f"past its own deadline for the upstream, so the broker or the path to it stalled "
            f"rather than Google: {exc}"
        ) from exc
    except OSError as exc:
        raise RelayError(f"could not reach the credential broker for {url}: {exc}") from exc
    if not HTTP_OK_FIRST <= response.status_code <= HTTP_OK_LAST:
        raise RelayError(describe_failure(url, response))
    try:
        return response.json()
    except ValueError as exc:
        raise RelayError(f"{url}: the response was not JSON: {exc}") from exc


def window(hours: int) -> tuple[str, str]:
    """The last `hours` as (start, end), spelt the way the APIs' filters take them."""
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)
    return start_time.strftime(TIMESTAMP_FORMAT), end_time.strftime(TIMESTAMP_FORMAT)


def get_paginated(
    session, url: str, *, params: dict, items_key: str, limit: int | None = None, whole: bool = False
) -> list:
    """The `items_key` entries of a paged list read, across pages.

    Follows `nextPageToken` until the list runs dry or `limit` items are in
    hand (when given), and returns at most `limit` items; a `limit` below
    MIN_LIMIT is a ValueError, since Google reads such a page size as a
    default page and the trim would discard it. A list still holding
    a token after MAX_LIST_PAGES pages raises RelayError when nothing was
    read, so the caller cannot report an empty window it never saw the end
    of, and otherwise returns what was read with a note on stderr -- unless
    `whole` is set, for a caller that sums the list (a partial sum is a
    wrong number stated fluently), in which case the cap is a RelayError
    whatever is in hand.
    """
    if limit is not None and limit < MIN_LIMIT:
        raise ValueError(f"limit must be at least {MIN_LIMIT} or None, got {limit}")
    items: list = []
    token = None
    for _ in range(MAX_LIST_PAGES):
        page_params = dict(params)
        if token:
            page_params[PAGE_TOKEN_PARAM] = token
        page = get_json(session, url, params=page_params)
        items.extend(page.get(items_key) or [])
        token = page.get(NEXT_PAGE_TOKEN_KEY)
        if not token or (limit is not None and len(items) >= limit):
            break
    else:
        note = PAGE_CAP_NOTE.format(pages=MAX_LIST_PAGES)
        if not items:
            raise RelayError(f"{url}: {note} and nothing in hand; the window may not be empty")
        if whole:
            raise RelayError(f"{url}: {note}; a sum over {len(items)} item(s) of a longer list would be wrong")
        print(f"warning: {url}: {note}; the {len(items)} item(s) returned are not the whole list", file=sys.stderr)
    return items if limit is None else items[:limit]


def list_traces(session, project_id: str, hours: int, page_size: int) -> dict:
    """The traces in the last `hours`, at most `page_size` of them, as `{"traces": [...]}`."""
    start_str, end_str = window(hours)
    params = {"startTime": start_str, "endTime": end_str, "pageSize": page_size}
    url = TRACE_LIST_URL.format(project=urllib.parse.quote(project_id, safe=""))
    return {"traces": get_paginated(session, url, params=params, items_key="traces", limit=page_size)}


def get_trace(session, project_id: str, trace_id: str) -> dict:
    """One trace with its spans."""
    url = TRACE_GET_URL.format(
        project=urllib.parse.quote(project_id, safe=""),
        trace_id=urllib.parse.quote(trace_id, safe=""),
    )
    return get_json(session, url)
