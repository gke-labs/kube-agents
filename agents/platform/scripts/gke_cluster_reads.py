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
         "workers": 16}
Output: {"lists": {"p1": {"status": 200, "clusters": [["name", "location"], ...],
                           "missingZones": [...]}
                         | {"status": <int or 0>, "error": {...}}},
         "gets":  {"project/location/name": {"status": 200} | {"status": ..., "error": {...}}}}

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parent))

import credential_proxy_client  # noqa: E402

GKE_API = "https://container.googleapis.com/v1"
# Per call: the client's connect bound, and a read bound under the broker's own
# upstream deadline (API_RELAY_DEADLINE_S, 120 s) so a slow page is the broker's 504.
CALL_TIMEOUT = (credential_proxy_client.BROKER_CONNECT_TIMEOUT_SECONDS, 150)
DEFAULT_WORKERS = 16
MAX_WORKERS = 32
# How much of an error body is passed back: enough for the Google error's
# status, reason and message, which is all the reconcile reads.
ERROR_BODY_CHARS = 2000

_local = threading.local()


def _session():
    # One requests session per worker thread: sharing one across threads is not
    # something requests promises to be safe.
    if getattr(_local, "session", None) is None:
        _local.session = credential_proxy_client.ApiSession()
    return _local.session


def _error_of(response) -> dict:
    text = (response.text or "")[:ERROR_BODY_CHARS]
    try:
        body = json.loads(text)
    except ValueError:
        return {"text": text}
    return body if isinstance(body, dict) else {"text": text}


def _call(url: str) -> tuple[int, dict | None, dict | None]:
    """(status, json body on 2xx, error on anything else)."""
    try:
        response = _session().get(url, timeout=CALL_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - reported, not raised: one read is one row
        return 0, None, {"exception": type(exc).__name__, "text": str(exc)[:ERROR_BODY_CHARS]}
    if 200 <= response.status_code < 300:
        try:
            body = response.json()
        except ValueError:
            return response.status_code, None, {"text": "the relay answered 2xx with a body that is not JSON"}
        return response.status_code, body if isinstance(body, dict) else None, None
    return response.status_code, None, _error_of(response)


def list_project(project: str) -> dict:
    status, body, error = _call(f"{GKE_API}/projects/{project}/locations/-/clusters")
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


def get_cluster(project: str, location: str, name: str) -> dict:
    status, _, error = _call(f"{GKE_API}/projects/{project}/locations/{location}/clusters/{name}")
    return {"status": status} if error is None else {"status": status, "error": error}


def run(request: dict) -> dict:
    workers = request.get("workers") or DEFAULT_WORKERS
    workers = max(1, min(MAX_WORKERS, int(workers)))
    projects = [p for p in request.get("projects") or [] if isinstance(p, str)]
    clusters = [tuple(c) for c in request.get("clusters") or []
                if isinstance(c, (list, tuple)) and len(c) == 3 and all(isinstance(x, str) for x in c)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        lists = dict(zip(projects, pool.map(list_project, projects)))
        gets = dict(zip(("/".join(c) for c in clusters), pool.map(lambda c: get_cluster(*c), clusters)))
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
