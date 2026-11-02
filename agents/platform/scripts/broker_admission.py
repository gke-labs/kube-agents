"""How wide a fleet sweep reads, from what the credential broker admits.

Every check a sweep runs is a kubectl or gcloud the broker executes, and the
broker admits a fixed number of commands at once: its slot cap, or what its
child memory budget fits at the container's effective memory limit, whichever is
smaller (credential_proxy.py, `CommandExecutor.admitted_requests`). A pool wider
than that only queues the rest at the broker, where a check still waiting at its
60 s admission bound is refused busy. So a sweep's width follows the broker's
admitted count, read from `GET /v1/exec/admission`, rather than a constant.

The count differs by install. At the operator's default 2Gi limit the budget
fits 9 and the slot cap of 8 binds. On GKE Autopilot without bursting the limit
is the 1Gi memory request, and the budget fits 4. Only the broker sees the
effective limit, so only the broker can say.

`sweep_width` leaves the session role's cap free (two by default; the broker
reports it) when the broker admits six or more, so a sweep doesn't take every
slot a live chat read would queue behind, and
otherwise reads at most four wide, today's width. The headroom never takes the
width below four either, so no install sweeps slower than it did. This doesn't coordinate sweeps that run at the same time: each
sizes its own pool, and the broker admits in arrival order, so live reads still
queue behind all of them. Putting live reads first takes priority admission in
the broker, which is separate work.

A broker that predates the route, or any failure to read it, gives today's
width of four.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request

import credential_proxy_client

ADMISSION_PATH = "/v1/exec/admission"
# The width before the broker could be asked, and the answer when it can't be.
FALLBACK_WIDTH = 4
# How many of the broker's slots a sweep leaves free once it admits enough to
# spare them: the session role's cap, the most one chat conversation's reads
# take at once. The broker reports its own; this is the default it ships with,
# used when the report doesn't carry one.
CHAT_HEADROOM = 2
# The admitted count at or above which the headroom is taken; below it the
# width stays at most FALLBACK_WIDTH, so no install sweeps slower than before.
HEADROOM_FROM = 6
# The admission read's socket timeout: urllib applies it to the connect and to
# each blocking read, not to the exchange as a whole. The broker client's own
# opener drops its socket timeout after connecting, on purpose, because an exec
# may run for minutes; this read is one small JSON body that arrives in a read or
# two, so a connection that goes half-open times out here instead of holding the
# sweep back before it reads a single cluster.
ADMISSION_TIMEOUT_S = 10
# Never follow a redirect out of the broker: urllib would re-send the
# Authorization header to wherever it points (credential_proxy_client._NoRedirect
# says why).
_OPENER = urllib.request.build_opener(credential_proxy_client._NoRedirect())

_cached_width: int | None = None


def width_for(admitted: int, headroom: int = CHAT_HEADROOM) -> int:
    """The sweep width for a broker that admits ``admitted`` commands at once
    and keeps ``headroom`` for a chat session's reads."""
    floor = max(1, min(FALLBACK_WIDTH, admitted))
    if admitted >= HEADROOM_FROM:
        # Never below today's width, however large a session cap the broker
        # reports: the headroom only takes from what this change added.
        return max(floor, admitted - headroom)
    return floor


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def read_report(endpoint: str | None = None) -> tuple[int, int] | None:
    """The broker's admitted count and session cap, or None when they can't be
    read. A report without a usable session cap gives CHAT_HEADROOM."""
    endpoint = (endpoint if endpoint is not None else os.environ.get("CREDENTIAL_PROXY_URL", "")).strip()
    if not endpoint:
        return None
    try:
        request = urllib.request.Request(
            endpoint.rstrip("/") + ADMISSION_PATH,
            headers=credential_proxy_client.authorization_headers(),
        )
        with _OPENER.open(request, timeout=ADMISSION_TIMEOUT_S) as response:
            report = json.load(response)
    except (
        urllib.error.URLError,
        OSError,
        ValueError,
        # A broker or intermediary that cuts the body short or mangles the
        # status line raises IncompleteRead or BadStatusLine, neither an
        # OSError (credential_proxy_client.BrokerDisconnected says the same).
        http.client.HTTPException,
        credential_proxy_client.TokenUnavailable,
    ):
        return None
    if not isinstance(report, dict):
        return None
    admitted = _positive_int(report.get("admitted"))
    if admitted is None:
        return None
    return admitted, _positive_int(report.get("session_cap")) or CHAT_HEADROOM


def sweep_width() -> int:
    """How many clusters a sweep reads at once on this install. Read once per
    process; FALLBACK_WIDTH when the broker can't say."""
    global _cached_width
    if _cached_width is None:
        report = read_report()
        _cached_width = FALLBACK_WIDTH if report is None else width_for(*report)
    return _cached_width
