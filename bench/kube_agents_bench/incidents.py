# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Read the event ledger of the install under test, for one workload.

The ledger is served by the Session KV server inside the agent pod as
``GET /v1/intercepted-events``, on loopback only, to a caller presenting the
pod's ``SESSION_KV_API_KEY``. So the read is one ``kubectl exec`` into the agent
container, as :mod:`kube_agents_bench.findings` reads the findings queue, and
for the same reason it goes through the route rather than the SQLite file.

A reply without the sentinel is a failed read, never an empty one: the shell
returns ``""`` on any kubectl failure.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from devops_bench.verification.base import VERIFIERS, VerificationStatus
from pydantic import Field

from kube_agents_bench import onboarding, transcript
from kube_agents_bench.findings import REQUEST_TIMEOUT_SEC, SESSION_KV_URL
from kube_agents_bench.verifiers import _NO_RUN_CLOCK_REASON, _NO_TRANSCRIPT_REASON, _OnboardingPollVerifier
from kube_agents_bench.worker_trajectory import FALLBACK_PYTHON, HERMES_PYTHON

__all__ = ["EVENTS_READ", "WorkloadIncidentFoldedVerifier", "read_workload"]

EVENTS_READ = "__INTERCEPTED_EVENTS_READ__"
# session_kv_server.py: FEED_EVENTS_MAX_LIMIT. Newest first, so the rows a run
# just wrote are in the page however long the ledger is.
ROW_LIMIT = 500
# The fields of a row a verdict reads; the rest stays in the pod.
ROW_FIELDS = ("id", "timestamp", "namespace", "kind", "name", "reason", "status", "duplicate_of")
# The feed statuses of a row the daemon admitted: posted (or dispatched
# headless), or posted and then refused by chat. session_kv_server.py anchors a
# workload's window on exactly these.
ADMITTED_STATUSES = frozenset({"injected", "undelivered"})
# Rows of the workload a verdict carries in its raw output, newest first: a
# repetition's burst and the one before it, without the whole day.
RAW_ROWS = 50
# `intercepted_events.created_at`, SQLite's CURRENT_TIMESTAMP: UTC, no zone.
LEDGER_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Runs in the agent container. Prints the sentinel and one JSON line: the rows
# for the workload, newest first, or the error that stopped the read.
_READ_SCRIPT = r"""
import json, os, sys, urllib.error, urllib.parse, urllib.request
base, namespace, name, limit, timeout, fields, sentinel = sys.argv[1:8]
out = {"events": None, "error": None}
token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
if not token:
    out["error"] = "SESSION_KV_API_KEY is not set in the agent container"
else:
    query = urllib.parse.urlencode({"limit": limit})
    request = urllib.request.Request(base + "/v1/intercepted-events?" + query, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            rows = json.loads(response.read().decode("utf-8")).get("events")
        if not isinstance(rows, list):
            out["error"] = "GET /v1/intercepted-events returned no list of events"
        else:
            out["events"] = [
                {k: row.get(k) for k in fields.split(",")}
                for row in rows
                if isinstance(row, dict) and row.get("namespace") == namespace and row.get("name") == name
            ]
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["error"] = "GET /v1/intercepted-events: %s" % exc
print(sentinel)
print(json.dumps(out))
"""


def read_command(namespace: str, name: str) -> str:
    """The ``sh -c`` line that reads the ledger's rows for one workload in the agent container."""
    args = " ".join(
        shlex.quote(a)
        for a in [
            SESSION_KV_URL,
            namespace,
            name,
            str(ROW_LIMIT),
            str(REQUEST_TIMEOUT_SEC),
            ",".join(ROW_FIELDS),
            EVENTS_READ,
        ]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_READ_SCRIPT)} {args}'
    )


def read_workload(
    shell: Callable[[str, float], str], namespace: str, name: str, timeout: float
) -> dict[str, Any] | None:
    """The ledger's rows for ``namespace``/``name``, newest first, or ``None`` if the pod could not be read.

    Returns ``{"events": [...], "error": None}``, or ``{"events": None,
    "error": "..."}`` when the pod answered but the server did not. ``shell`` is
    :func:`kube_agents_bench.onboarding.agent_shell`, a parameter so the tests
    can run the command locally.
    """
    lines = shell(read_command(namespace, name), timeout).splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == EVENTS_READ), None)
    if start is None:
        return None
    try:
        parsed = json.loads("\n".join(lines[start + 1 :]))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("error"):
        return {"events": None, "error": str(parsed["error"])}
    if not isinstance(parsed.get("events"), list):
        return None
    return {"events": parsed["events"], "error": None}


def _ledger_time(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), LEDGER_TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@VERIFIERS.register("workload_incident_folded")
class WorkloadIncidentFoldedVerifier(_OnboardingPollVerifier):
    """Checks that one workload's burst of events became one incident.

    Reads the ledger rows for ``namespace``/``name`` of object ``kind`` through
    the Session KV server's ``GET /v1/intercepted-events`` in the agent
    container. The anchor is the newest admitted row (status ``injected`` or
    ``undelivered``). Passes when at least ``min_folded`` rows name it in
    ``duplicate_of``. ``kind`` matters because the daemon folds within one
    object kind, and other producers share the ledger under the same name: the
    drift detector records the stack's own Deployment apply as kind
    ``deployments``, often seconds before the burst.

    Keyed on the newest admitted row rather than on a time window, because the
    stack plants its burst before the agent turn starts, and the previous
    repetition re-plants the same workload a few minutes earlier, so no window
    measured from ``TranscriptSnapshot.started_at`` separates the two. The time
    is still checked once: an anchor older than ``started_at`` less
    ``max_age_sec`` is from an earlier run, and is a fail.

    No admitted row, a stale anchor, or too few folded rows is a fail; a daemon
    that does not fold at all leaves every replica's row admitted and the newest
    with nothing folded into it. An unreadable pod, a server that refused the
    read, or a run with no start time is ``status="error"``.
    """

    type: Literal["workload_incident_folded"]
    namespace: str = Field(min_length=1)
    name: str = Field(min_length=1)
    kind: str = Field(default="Pod", min_length=1)
    min_folded: int = Field(ge=1)
    # The stack's own budget for raising the incident (a 420s debounce wait and
    # a 300s wait for the watcher in bench/tf/prebuilt/autoops-incident), plus
    # the runner and the agent pod being two clocks.
    max_age_sec: float = Field(default=900.0, gt=0)

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        snap = transcript.get()
        if snap is None:
            return "error", _NO_TRANSCRIPT_REASON, None
        if not snap.started_at:
            return "error", _NO_RUN_CLOCK_REASON, None
        since = datetime.fromtimestamp(snap.started_at, tz=timezone.utc) - timedelta(seconds=self.max_age_sec)
        read = read_workload(onboarding.agent_shell, self.namespace, self.name, read_timeout)
        if read is None:
            return "error", "the agent pod could not be read (kubectl exec failed or the command did not run)", None
        if read["error"]:
            return "error", f"the event ledger could not be read: {read['error']}", None
        rows = sorted(
            (row for row in read["events"] if row.get("kind") == self.kind),
            key=lambda row: int(row.get("id") or 0),
            reverse=True,
        )
        workload = f"{self.namespace}/{self.name}"
        admitted = [row for row in rows if row.get("status") in ADMITTED_STATUSES]
        recent = [row for row in admitted if (_ledger_time(row.get("timestamp")) or since) > since]
        raw = {"rows": rows[:RAW_ROWS]}
        if not admitted:
            return "fail", f"the ledger holds no admitted incident for {workload}", raw
        anchor = admitted[0]
        anchored_at = _ledger_time(anchor.get("timestamp"))
        if anchored_at is None or anchored_at < since:
            return (
                "fail",
                f"the newest incident for {workload} (ledger row {anchor.get('id')}, {anchor.get('timestamp')}) "
                f"is older than this run's window, which opens at {since:%Y-%m-%d %H:%M:%S} UTC",
                raw,
            )
        folded = [row for row in rows if row.get("duplicate_of") == anchor.get("id")]
        summary = (
            f"the newest incident for {workload} (ledger row {anchor.get('id')}) has {len(folded)} event(s) "
            f"folded into it, and {len(recent)} incident(s) were admitted for it in this run's window"
        )
        if len(folded) < self.min_folded:
            return "fail", f"{summary}; at least {self.min_folded} folded event(s) were expected", raw
        return "pass", summary, raw
