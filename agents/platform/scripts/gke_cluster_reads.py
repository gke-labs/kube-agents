#!/usr/bin/env python3
"""The Cluster Agent reconcile's GKE reads, in one sandbox process (#3703).

The reconcile runs in the agent pod, but only the shell sandbox holds the token the
broker's read-only Cloud API relay (`/v1/gcp/`) accepts. So the reconcile sends this
script one JSON document on stdin, naming the projects to list and the clusters to
look up, and reads one JSON document back on stdout. Every read is a REST call through
`credential_proxy_client.ApiSession`, checked against `api_policy.API_READ_ROUTES`
(`gcp.api.container.clusters-list` and `-get`) and projected by the broker to name,
location, status and id. That replaces a `gcloud` process per project and per cluster,
whose start-up on the sandbox's CPU was what made a tick grow by ~2.4 s per cluster.

Input:  {"projects": ["p1", ...], "clusters": [["project", "location", "name"], ...],
         "workers": 16, "budget_seconds": 25.0}
Output: {"lists": {"p1": {"status": 200, "clusters": [["name", "location"], ...],
                           "missingZones": [...]}
                         | {"status": <int or 0>, "error": {...}}},
         "gets":  {"project/location/name": {"status": 200} | {"status": ..., "error": {...}}}}

`budget_seconds` is this process's own deadline: a call starts only with time left in
it, its connect and read timeouts together fit in what is left (a read timeout bounds
each wait for bytes, and the broker writes its answer at once), and one it never started comes back as
`{"status": 0, "error": {"skipped": "budget"}}` for the caller to read some other way.
So the answer arrives inside the caller's wait however the calls are spread over the
worker threads, and a slow call costs that one read.

This reports what each call answered and decides nothing: which answer means "gone",
"denied" or "unknown" is the reconcile's call (`cluster_agent_reconcile._read_*`), so
the rule that only a NotFound deletes lives in one place. `status` 0 means the call
never got an HTTP answer (a connect failure, a timeout); `error` then carries the
exception's type and text.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# The reconcile runs this as `hermes`, the trusted sandbox login, so it runs the
# root-owned copy under /opt/vcs/libexec/platform, staged with its import closure
# (deploy/sandbox/Dockerfile, deploy/sandbox/trusted-closure-guard.py). That copy
# leaves the agent-writable script directories off its path, as vcs_client.py does;
# a copy anywhere else finds its siblings there.
TRUSTED_CLOSURE = "/opt/vcs/libexec/platform"
SANDBOX_GKE_READS = f"{TRUSTED_CLOSURE}/gke_cluster_reads.py"
if not str(Path(__file__).resolve()).startswith(TRUSTED_CLOSURE + "/"):
    sys.path.append("/opt/defaults/scripts")
    sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parent))

import credential_proxy_client  # noqa: E402

GKE_API = "https://container.googleapis.com/v1"
# Per call: the client's connect bound, and a read bound of what is left of the
# budget, never more than this. That sits over the broker's own upstream deadline
# (API_RELAY_DEADLINE_S, 120 s), so a slow page is the broker's 504.
MAX_READ_TIMEOUT = 150.0
# A call is not started with less than this left: it could not finish.
MIN_CALL_SECONDS = 1.0
# The budget when the caller names none.
DEFAULT_BUDGET_SECONDS = MAX_READ_TIMEOUT
DEFAULT_WORKERS = 16
MAX_WORKERS = 32
# How much of an error body that is not JSON is passed back. A JSON error is parsed
# whole and passed back whole, so a long one keeps the reason the reconcile reads.
ERROR_BODY_CHARS = 2000

_local = threading.local()


def _session():
    # One requests session per worker thread: sharing one across threads is not
    # something requests promises to be safe.
    if getattr(_local, "session", None) is None:
        _local.session = credential_proxy_client.ApiSession()
    return _local.session


def _error_of(response) -> dict:
    text = response.text or ""
    try:
        body = json.loads(text)
    except ValueError:
        return {"text": text[:ERROR_BODY_CHARS]}
    return body if isinstance(body, dict) else {"text": text[:ERROR_BODY_CHARS]}


def _call(url: str, deadline: float) -> tuple[int, dict | None, dict | None]:
    """(status, json body on 2xx, error on anything else)."""
    left = deadline - time.monotonic()
    if left < MIN_CALL_SECONDS:
        return 0, None, {"skipped": "budget"}
    # The broker is a ClusterIP a hop away: the connect gets at most half of what is
    # left, and the read the rest, so the two never pass the deadline.
    connect = min(credential_proxy_client.BROKER_CONNECT_TIMEOUT_SECONDS, left / 2)
    timeout = (connect, min(MAX_READ_TIMEOUT, left - connect))
    try:
        response = _session().get(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - reported, not raised: one read is one row
        return 0, None, {"exception": type(exc).__name__, "text": str(exc)[:ERROR_BODY_CHARS]}
    if 200 <= response.status_code < 300:
        try:
            body = response.json()
        except ValueError:
            return response.status_code, None, {"text": "the relay answered 2xx with a body that is not JSON"}
        return response.status_code, body if isinstance(body, dict) else None, None
    return response.status_code, None, _error_of(response)


def list_project(project: str, deadline: float) -> dict:
    status, body, error = _call(f"{GKE_API}/projects/{project}/locations/-/clusters", deadline)
    if error is not None or body is None:
        return {"status": status, "error": error or {"text": "no body"}}
    rows = []
    for cluster in body.get("clusters") or []:
        if isinstance(cluster, dict) and isinstance(cluster.get("name"), str) and isinstance(cluster.get("location"), str):
            rows.append([cluster["name"], cluster["location"]])
    answer = {"status": status, "clusters": rows}
    missing = body.get("missingZones")
    if isinstance(missing, list):
        answer["missingZones"] = [z for z in missing if isinstance(z, str)]
    return answer


def get_cluster(project: str, location: str, name: str, deadline: float) -> dict:
    status, _, error = _call(f"{GKE_API}/projects/{project}/locations/{location}/clusters/{name}", deadline)
    return {"status": status} if error is None else {"status": status, "error": error}


def run(request: dict) -> dict:
    workers = request.get("workers") or DEFAULT_WORKERS
    workers = max(1, min(MAX_WORKERS, int(workers)))
    budget = request.get("budget_seconds")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
        budget = DEFAULT_BUDGET_SECONDS
    deadline = time.monotonic() + float(budget)
    projects = [p for p in request.get("projects") or [] if isinstance(p, str)]
    clusters = [tuple(c) for c in request.get("clusters") or []
                if isinstance(c, (list, tuple)) and len(c) == 3 and all(isinstance(x, str) for x in c)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        lists = dict(zip(projects, pool.map(lambda p: list_project(p, deadline), projects)))
        gets = dict(zip(("/".join(c) for c in clusters), pool.map(lambda c: get_cluster(*c, deadline), clusters)))
    return {"lists": lists, "gets": gets}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        print(f"gke_cluster_reads: the request on stdin is not JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(request, dict):
        print("gke_cluster_reads: the request on stdin must be a JSON object", file=sys.stderr)
        return 2
    json.dump(run(request), sys.stdout, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
