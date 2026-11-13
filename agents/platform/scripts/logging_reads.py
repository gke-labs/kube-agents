#!/usr/bin/env python3
"""Cloud Logging reads for the collectors, through the credential broker's relay.

Every `gcloud logging read` starts a `gcloud` process in the credential broker.
The broker's Cloud API relay serves the same read, Logging `entries:list`,
without one (`api_policy.LOGGING_ENTRIES_LIST_RULE`). `read_json` sends the
requests `gcloud logging read FILTER --project P --limit N --format json`
sends, page for page, and returns the entries it prints: newest first, as a
JSON list.

It returns None whenever the relay does not give that answer, and the caller
then runs its `gcloud` command as before. So a broker older than the route, a
relay that is off, a refusal, an upstream error and a pod with no broker all
read exactly as they did; the relay only ever replaces a read that worked. A
read that does not finish in the caller's time is `ReadTimedOut`, which is the
outcome the `gcloud` command would have had too.

Standard library only at import: the relay client, and `requests` behind it,
are imported on use, in the sandbox where both are.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

ENTRIES_LIST_URL = "https://logging.googleapis.com/v2/entries:list"
# What marks the shell sandbox, the one pod whose reads go through the broker
# (credential_proxy_client.BROKER_URL_ENV, spelled here so the import can wait).
BROKER_URL_ENV = "CREDENTIAL_PROXY_URL"
# The order `gcloud logging read` asks for by default.
ORDER_NEWEST_FIRST = "timestamp desc"
# Entries asked for per request: the API's ceiling, and what `gcloud` asks for,
# so a read costs the project's `entries.list` request quota what it did. The
# API itself answers with shorter pages when the entries are large.
PAGE_SIZE = 1000
HTTP_OK = 200
# The request and answer fields of `entries:list`.
RESOURCE_NAMES_KEY = "resourceNames"
FILTER_KEY = "filter"
ORDER_BY_KEY = "orderBy"
PAGE_SIZE_KEY = "pageSize"
PAGE_TOKEN_KEY = "pageToken"
ENTRIES_KEY = "entries"
NEXT_PAGE_TOKEN_KEY = "nextPageToken"
PROJECT_RESOURCE = "projects/{project}"
# The bound `gcloud logging read --freshness` puts in front of the filter, and
# the word whose presence in the filter makes `gcloud` add no bound.
FRESHNESS_CLAUSE = 'timestamp>="{start}"'
FRESHNESS_SKIP_WORD = "timestamp"
CLAUSE_JOIN = " AND "
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# One session for the process: it remembers a move to the stateful broker, and
# the collectors share it across their threads as they share `ApiSession`.
_session = None


class ReadTimedOut(TimeoutError):
    """The read did not finish in the caller's time."""


def freshness_filter(filter_text: str, freshness: timedelta, now: datetime | None = None) -> str:
    """The filter `gcloud logging read FILTER --freshness=<freshness>` sends.

    `gcloud` computes the bound itself and puts it first: `timestamp>="<now
    less freshness>" AND FILTER`. It adds no bound to a filter whose text
    already contains the word `timestamp`, and neither does this. `now` is for
    tests.
    """
    if FRESHNESS_SKIP_WORD in filter_text:
        return filter_text
    start = (now or datetime.now(timezone.utc)) - freshness
    return FRESHNESS_CLAUSE.format(start=start.strftime(TIMESTAMP_FORMAT)) + CLAUSE_JOIN + filter_text


def _relay_session():
    global _session
    if _session is None:
        import credential_proxy_client

        _session = credential_proxy_client.ApiSession()
    return _session


def read_json(
    project: str,
    filter_text: str,
    limit: int,
    *,
    timeout_s: float,
    session=None,
    clock=time.monotonic,
) -> str | None:
    """Up to `limit` entries of `project` matching `filter_text`, newest first, as JSON text.

    The text is a JSON list, `[]` when nothing matched. None when the relay did
    not give the whole answer: no broker here, a request that failed or was
    refused, or an answer that is not a page of entries. Nothing partial is
    returned. Raises `ReadTimedOut` when `timeout_s`, the budget for every
    page together, is spent first. `session` and `clock` are for tests.
    """
    if session is None and not os.environ.get(BROKER_URL_ENV):
        return None
    deadline = clock() + timeout_s
    entries: list = []
    token = None
    try:
        if session is None:
            session = _relay_session()
        while len(entries) < limit:
            left = deadline - clock()
            if left <= 0:
                raise ReadTimedOut(f"timed out after {timeout_s} seconds")
            body = {
                RESOURCE_NAMES_KEY: [PROJECT_RESOURCE.format(project=project)],
                FILTER_KEY: filter_text,
                ORDER_BY_KEY: ORDER_NEWEST_FIRST,
                PAGE_SIZE_KEY: min(PAGE_SIZE, limit - len(entries)),
            }
            if token:
                body[PAGE_TOKEN_KEY] = token
            response = session.post(ENTRIES_LIST_URL, json=body, timeout=left)
            if response.status_code != HTTP_OK:
                return None
            answer = response.json()
            page = answer.get(ENTRIES_KEY, []) if isinstance(answer, dict) else None
            if not isinstance(page, list) or not all(isinstance(entry, dict) for entry in page):
                return None
            entries.extend(page)
            token = answer.get(NEXT_PAGE_TOKEN_KEY)
            if not token:
                break
            if not isinstance(token, str):
                return None
    except ReadTimedOut:
        raise
    except Exception as fault:  # noqa: BLE001 -- any other fault is "the relay did not answer"; gcloud is the fallback
        if deadline - clock() <= 0:
            # A request that ran into the end of the budget: the client's own timeout.
            raise ReadTimedOut(f"timed out after {timeout_s} seconds") from fault
        return None
    return json.dumps(entries[:limit])
