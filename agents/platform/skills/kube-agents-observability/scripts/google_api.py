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

# How long one relayed read may take end to end. Below the broker's own
# API_RELAY_DEADLINE_S, so a slow upstream is reported by the helper's message
# rather than by a broker 504.
RELAY_TIMEOUT_SECONDS = 60

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


def get_json(session, url: str, *, params=None) -> dict:
    """One relayed GET, decoded; raises RelayError with the reason on anything else."""
    try:
        response = session.get(url, params=params, timeout=RELAY_TIMEOUT_SECONDS)
    except credential_proxy_client.TokenUnavailable as exc:
        raise RelayError(f"no caller token for the credential broker: {exc}") from exc
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


def list_traces(session, project_id: str, hours: int, page_size: int) -> dict:
    """The traces in the last `hours`, at most `page_size` of them."""
    start_str, end_str = window(hours)
    params = {"startTime": start_str, "endTime": end_str, "pageSize": page_size}
    url = TRACE_LIST_URL.format(project=urllib.parse.quote(project_id, safe=""))
    return get_json(session, url, params=params)


def get_trace(session, project_id: str, trace_id: str) -> dict:
    """One trace with its spans."""
    url = TRACE_GET_URL.format(
        project=urllib.parse.quote(project_id, safe=""),
        trace_id=urllib.parse.quote(trace_id, safe=""),
    )
    return get_json(session, url)
