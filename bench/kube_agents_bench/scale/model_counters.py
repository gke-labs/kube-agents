"""The model's own counters for a period, independent of the install's session records.

Vertex AI counts every token the project sends (`token_count`, by model and input or output),
and the Service Usage metrics count every request by the service account that made it. The
first checks that the session records hold all the tokens; the second tells which install, or
which other caller such as an eval judge, made the calls, so a token outside every session is
a named line rather than a gap.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from kube_agents_bench.scale import collect, kube

TOKEN_METRIC = "aiplatform.googleapis.com/publisher/online_serving/token_count"
REQUEST_METRIC = "serviceruntime.googleapis.com/api/request_count"
VERTEX_SERVICE = "aiplatform.googleapis.com"
SERVICE_ACCOUNT_PREFIX = "serviceaccount:"
TIMEOUT_SECONDS = 120
# Points a minute apart, each kept when its minute ends inside the period: one point spanning the
# whole period is aligned by Monitoring to its own boundaries and takes in time outside it.
ALIGNMENT_SECONDS = 60


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _query(project: str, flt: str, start: float, end: float, group: list[str]) -> list[dict[str, Any]]:
    """One sum over [start, end), grouped; application-default credentials, as collect.py reads."""
    token = kube.run([kube.GCLOUD, "auth", "application-default", "print-access-token"]).strip()
    params = {"filter": flt, "interval.startTime": _rfc3339(start), "interval.endTime": _rfc3339(end),
              "aggregation.alignmentPeriod": f"{ALIGNMENT_SECONDS}s",
              "aggregation.perSeriesAligner": "ALIGN_SUM", "aggregation.crossSeriesReducer": "REDUCE_SUM",
              "aggregation.groupByFields": group}
    base = collect.MONITORING_URL.format(project=project)
    url, series = base + "?" + urllib.parse.urlencode(params, doseq=True), []
    while url:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                       collect.QUOTA_PROJECT_HEADER: project})
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310 -- fixed host
            page = json.loads(response.read())
        series += page.get("timeSeries", [])
        nxt = page.get("nextPageToken")
        url = base + "?" + urllib.parse.urlencode({**params, "pageToken": nxt}, doseq=True) if nxt else ""
    return series


def _total(series: dict[str, Any], start: float = float("-inf"), end: float = float("inf")) -> float:
    """The series' sum over the points whose minute ends inside (start, end]."""
    total = 0.0
    for p in series["points"]:
        ends = datetime.fromisoformat(p["interval"]["endTime"].replace("Z", "+00:00")).timestamp()
        if start < ends <= end:
            total += float(p["value"].get("int64Value") or p["value"].get("doubleValue") or 0)
    return total


def vertex_tokens(project: str, start: float, end: float) -> dict[str, dict[str, int]]:
    """Tokens Vertex AI counted in the period, by model: {"gemini-3.5-flash": {"input": n, "output": n}}."""
    out: dict[str, dict[str, int]] = {}
    for s in _query(project, f'metric.type="{TOKEN_METRIC}"', start, end,
                    ["metric.label.type", "resource.label.model_user_id"]):
        model = s["resource"]["labels"].get("model_user_id", "")
        out.setdefault(model, {"input": 0, "output": 0})[s["metric"]["labels"]["type"]] = int(_total(s, start, end))
    return out


def service_accounts(project: str) -> dict[str, str]:
    """The project's service accounts, unique id to email: the request metric names callers by id."""
    out = subprocess.run([kube.GCLOUD, "iam", "service-accounts", "list", f"--project={project}",
                          "--format=json(uniqueId,email)"], capture_output=True, text=True, check=True).stdout
    return {a["uniqueId"]: a["email"] for a in json.loads(out or "[]")}


def requests_by_account(project: str, start: float, end: float) -> dict[str, int]:
    """Vertex AI requests in the period by the account that made them; an id outside the project stays an id."""
    names = service_accounts(project)
    flt = (f'metric.type="{REQUEST_METRIC}" AND resource.type="consumed_api" '
           f'AND resource.label.service="{VERTEX_SERVICE}"')
    out: dict[str, int] = {}
    for s in _query(project, flt, start, end, ["resource.label.credential_id"]):
        caller = s["resource"]["labels"].get("credential_id", "").removeprefix(SERVICE_ACCOUNT_PREFIX)
        out[names.get(caller, caller)] = out.get(names.get(caller, caller), 0) + int(_total(s, start, end))
    return out


def against_sessions(vertex: dict[str, dict[str, int]], rows: dict[str, dict[str, int]], model: str) -> dict[str, Any]:
    """The session rows' input and output against Vertex AI's count for the install's model."""
    ours = {"input": sum(r["input"] + r["cached"] for r in rows.values()),
            "output": sum(r["output"] for r in rows.values())}
    theirs = vertex.get(model, {"input": 0, "output": 0})
    ratio = {k: round(ours[k] / theirs[k], 4) if theirs.get(k) else None for k in ours}
    others = {m: v for m, v in vertex.items() if m != model}
    return {"model": model, "sessions": ours, "vertex": theirs, "ratio": ratio, "other_models": others}
