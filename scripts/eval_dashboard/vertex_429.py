#!/usr/bin/env python3
"""The pool's Vertex AI 429 rate, from Cloud Monitoring, for the storm rule.

The harness marks a repetition that died before the agent ran with a fixed
phrase and never the HTTP status, so the same phrases cover a quota storm and
a batch of teardown deaths. What tells them apart is outside the runs: per
pool project, Cloud Monitoring counts every Vertex AI call by response code
(`serviceruntime.googleapis.com/api/request_count` on the `consumed_api`
resource for `aiplatform.googleapis.com`). This reads it for every pool
project over the last few hours in ten-minute buckets and publishes one
document health.py's rule 2 reads: a storm needs the pool-wide 429 share over
a threshold; without a reading the rule keeps today's behaviour and says the
storm is unconfirmed.

Identity: the token command's account, `gcloud auth print-access-token` in
the workflow (the dashboard publisher), which needs `roles/monitoring.viewer`
on each pool project (docs/ci-health.md, "The 429 reading"). A project that
refuses the read is recorded with its error and the rest are summed.

Stdlib only, like the rest of this directory.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from . import fixture_state
except ImportError:  # imported with scripts/eval_dashboard/ itself on sys.path
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import fixture_state

SCHEMA_VERSION = 1
MONITORING_ROOT = "https://monitoring.googleapis.com/v3"
# The Vertex AI calls a pool project made, by HTTP response code.
METRIC_FILTER = 'metric.type="serviceruntime.googleapis.com/api/request_count" AND resource.type="consumed_api" AND resource.labels.service="aiplatform.googleapis.com"'
RESPONSE_CODE_LABEL = "response_code"
RATE_LIMITED_CODE = "429"
# Ten-minute buckets over the last six hours: the storm rule's two-hour
# window (health.STORM_WINDOW), the three hours of model calls a hit run
# makes before it finishes (health.STORM_429_LOOKBACK), and the hour a
# reading kept from an earlier blind tick may trail the runs' window by
# (health.STORM_429_READ_SLACK); the rule rejects a reading that falls short
# of that, and a test pins the sum. A fresh read ends at --now, the data's
# clock, so it always covers.
BUCKET_SECONDS = 600
LOOKBACK = timedelta(hours=6)
# A blind read keeps the previous reading only while the rule would still
# judge on it (health.VERTEX_429_MAX_AGE, pinned by a test); past that the
# blind document is written, so the published errors and read_at stay current.
KEEP_MAX_AGE = timedelta(hours=3)
# One Monitoring call per project, READ_WORKERS at a time; a whole-pool
# read must fit the workflow step (40 projects, 20 s each, 8 abreast: 100 s
# at worst).
CALL_TIMEOUT_S = 20
READ_WORKERS = 8
# Where the pool's project list lives (fixture_state reads the same file).
CI_DEPLOY_FILE = pathlib.Path(__file__).resolve().parents[2] / "hack" / "ci-deploy.sh"
DEFAULT_TOKEN_COMMAND = "gcloud auth print-access-token"
TOKEN_TIMEOUT_S = 60
EXIT_USAGE = 2


def log(message: str) -> None:
    print(message, file=sys.stderr)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds") if value else None


def parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def access_token(command: str, runner=subprocess.run) -> str | None:
    try:
        proc = runner(command.split(), capture_output=True, text=True, timeout=TOKEN_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"warning: {command}: {exc}")
        return None
    token = (proc.stdout or "").strip()
    if proc.returncode != 0 or not token:
        log(f"warning: {command} failed (exit {proc.returncode})")
        return None
    return token


def query_url(project: str, start: datetime, end: datetime) -> str:
    params = {
        "filter": METRIC_FILTER,
        "interval.startTime": iso(start),
        "interval.endTime": iso(end),
        "aggregation.alignmentPeriod": f"{BUCKET_SECONDS}s",
        "aggregation.perSeriesAligner": "ALIGN_DELTA",
        "aggregation.crossSeriesReducer": "REDUCE_SUM",
        "aggregation.groupByFields": f"metric.labels.{RESPONSE_CODE_LABEL}",
    }
    return f"{MONITORING_ROOT}/projects/{project}/timeSeries?{urllib.parse.urlencode(params)}"


def read_project(project: str, start: datetime, end: datetime, token: str, opener=urllib.request.urlopen) -> tuple[dict | None, str | None]:
    """The project's time series, or None with the error (a 403 is the grant)."""
    request = urllib.request.Request(query_url(project, start, end), headers={"Authorization": f"Bearer {token}"})
    try:
        with opener(request, timeout=CALL_TIMEOUT_S) as response:
            document = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        # HTTPException covers a body cut short of its length (IncompleteRead),
        # which is not an OSError; one project's answer must not end the read.
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(document, dict):
        return None, "not a JSON object"
    return document, None


def buckets_from(document: dict) -> dict[str, dict[str, int]]:
    """{bucket end ISO: {"requests": n, "429": n}} from one project's response."""
    buckets: dict[str, dict[str, int]] = {}
    for series in document.get("timeSeries") or []:
        if not isinstance(series, dict):
            continue
        code = str(((series.get("metric") or {}).get("labels") or {}).get(RESPONSE_CODE_LABEL) or "")
        for point in series.get("points") or []:
            if not isinstance(point, dict):
                continue
            end = ((point.get("interval") or {}).get("endTime"))
            value = (point.get("value") or {}).get("int64Value")
            try:
                count = int(value)
            except (TypeError, ValueError):
                continue
            if not isinstance(end, str):
                continue
            bucket = buckets.setdefault(end, {"requests": 0, RATE_LIMITED_CODE: 0})
            bucket["requests"] += count
            if code == RATE_LIMITED_CODE:
                bucket[RATE_LIMITED_CODE] += count
    return buckets


def merge(into: dict[str, dict[str, int]], more: dict[str, dict[str, int]]) -> None:
    for end, counts in more.items():
        bucket = into.setdefault(end, {"requests": 0, RATE_LIMITED_CODE: 0})
        bucket["requests"] += counts["requests"]
        bucket[RATE_LIMITED_CODE] += counts[RATE_LIMITED_CODE]


def read_pool(projects: list[str], now: datetime, token: str | None, opener=urllib.request.urlopen) -> dict:
    """The document: pool-wide ten-minute buckets, which projects were read,
    and the error per project that was not."""
    start, end = now - LOOKBACK, now
    pool: dict[str, dict[str, int]] = {}
    read: list[str] = []
    errors: dict[str, str] = {}
    if token is None:
        errors = {project: "no access token" for project in projects}
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=READ_WORKERS) as workers:
            answers = list(workers.map(lambda project: read_project(project, start, end, token, opener), projects))
        for project, (document, err) in zip(projects, answers):
            if err or document is None:
                errors[project] = err or "no document"
                continue
            read.append(project)
            merge(pool, buckets_from(document))
    return {
        "schema_version": SCHEMA_VERSION,
        "read_at": iso(now),
        "window_start": iso(start),
        "window_end": iso(end),
        "bucket_seconds": BUCKET_SECONDS,
        "projects": len(projects),
        "read": read,
        "errors": errors,
        "buckets": [{"end": end_at, **counts} for end_at, counts in sorted(pool.items())],
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=pathlib.Path, required=True, help="where to write vertex-429.json")
    parser.add_argument("--ci-deploy", type=pathlib.Path, default=CI_DEPLOY_FILE, help="hack/ci-deploy.sh, for the pool's project list")
    parser.add_argument("--token-command", default=DEFAULT_TOKEN_COMMAND, help="prints the bearer token to read Monitoring with")
    parser.add_argument("--now", type=parse_iso, default=None, help="the clock the window ends at (ISO 8601; the workflow passes data.json's generated_at, so the reading ends where the runs' window does; default: now)")
    return parser.parse_args(argv)


def previous_reading_is_usable(path: pathlib.Path, now: datetime) -> bool:
    """Whether the document at `path` is a reading that reached a project and
    is young enough (KEEP_MAX_AGE before `now`) for the rule to judge on."""
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(previous, dict) or not previous.get("read"):
        return False
    read_at = parse_iso(previous.get("read_at"))
    return read_at is not None and now - read_at <= KEEP_MAX_AGE


def main(argv=None, runner=subprocess.run, opener=urllib.request.urlopen) -> int:
    args = parse_args(argv)
    try:
        projects = fixture_state.pool_projects(args.ci_deploy.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # unreadable, or no project mapping in it
        log(f"error: {args.ci_deploy}: {exc}")
        return EXIT_USAGE
    now = args.now or datetime.now(timezone.utc)
    document = read_pool(projects, now, access_token(args.token_command, runner), opener)
    log(f"vertex 429: read {len(document['read'])} of {document['projects']} projects, {len(document['buckets'])} buckets; {len(document['errors'])} error(s)")
    if not document["read"] and previous_reading_is_usable(args.out, now):
        # A read that reached no project (no token, Monitoring down) does not
        # replace a previous reading that did, which the workflow fetched to
        # --out: the rule bounds that one itself, and a storm it denied stays
        # denied rather than re-declared on a blind tick. A previous reading
        # that was itself blind, or is older than the rule judges on, is
        # replaced, so the published errors and read_at stay current.
        log(f"vertex 429: kept the previous reading at {args.out}")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
