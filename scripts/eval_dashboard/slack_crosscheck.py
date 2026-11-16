#!/usr/bin/env python3
"""The Slack lane's canary overhead beside what real users get, once a week.

The Slack lane (hack/slack-lane/run.sh) measures Slack's overhead on a quiet
CI channel, and the nightly pages add it to each case's agent time
(slack_estimate.py). This checks that floor against the long-lived
environments: the same three gateway histograms, read from each
environment's Managed Service for Prometheus over the last seven days,
beside the lane's nightly values over the same days (data.json's
``slack_overhead``). The lane's quantiles are the mean of its nightly ones,
an approximation the document names.

It runs at most once per WINDOW: a complete previous document younger than
that ends the run with one log line. A project that refuses the read (HTTP
403: the publisher has no roles/monitoring.viewer there yet) is skipped with
one log line, and any other failure is logged and recorded as that
environment's error. A run that read no environment writes nothing; one that
read only some writes ``complete: false``, which does not count as the week's
run, so every tick tries again until each environment reads, and a grant
takes effect on the next tick.

Identity: the token command's account (the dashboard publisher in
ci-health.yml), as vertex_429.py's.

Usage: slack_crosscheck.py --data data.json --env NAME=PROJECT ... --prior prev.json --out out.json
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import pathlib
import statistics
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from .vertex_429 import access_token, iso, log, parse_iso
except ImportError:  # imported with scripts/eval_dashboard/ itself on sys.path
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from vertex_429 import access_token, iso, log, parse_iso

SCHEMA_VERSION = 1
WINDOW = timedelta(days=7)
PROMQL_ROOT = "https://monitoring.googleapis.com/v1/projects/{project}/location/global/prometheus/api/v1/query"
# The environments' gateway runs on the host cluster; their projects also
# hold clusters other installs create, whose gateways are not users' traffic.
CLUSTER_LABEL = 'cluster="platform-agent-host"'
PREFIX = "kubeagents_a2a_gateway_"
LAG = PREFIX + "slack_ingress_lag_seconds"
API = PREFIX + "slack_api_request_duration_seconds"
ACK = PREFIX + "slack_first_ack_seconds"
POST_LABELS = 'method="chat.postMessage",outcome="ok"'
KEYS = ("delivery_lag_s", "post_s", "first_ack_p50_s", "first_ack_p95_s")
P50, P95 = 0.5, 0.95
CALL_TIMEOUT_S = 20
FORBIDDEN = 403
DEFAULT_TOKEN_COMMAND = "gcloud auth print-access-token"
ROUND_TO = 3
RATIO_ROUND_TO = 2


def _increase(name: str, labels: str = "") -> str:
    selector = ",".join(x for x in (CLUSTER_LABEL, labels) if x)
    return f"sum(increase({name}{{{selector}}}[{WINDOW.days}d]))"


def _quantile(q: float) -> str:
    return f"histogram_quantile({q}, sum by (le) (increase({ACK}_bucket{{{CLUSTER_LABEL}}}[{WINDOW.days}d])))"


QUERIES = {
    "delivery_lag_s": f"{_increase(LAG + '_sum')} / {_increase(LAG + '_count')}",
    "post_s": f"{_increase(API + '_sum', POST_LABELS)} / {_increase(API + '_count', POST_LABELS)}",
    "first_ack_p50_s": _quantile(P50),
    "first_ack_p95_s": _quantile(P95),
    "deliveries": _increase(LAG + "_count"),
}


class Forbidden(Exception):
    pass


def _value(document) -> float | None:
    """The single sample of an instant query's vector, or None (no data, NaN, Inf)."""
    try:
        result = ((document or {}).get("data") or {}).get("result") or []
        value = float(result[0]["value"][1])
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None
    return round(value, ROUND_TO) if math.isfinite(value) else None


def read_environment(project: str, now: datetime, token: str, opener=urllib.request.urlopen) -> dict:
    """The environment's seven-day values; raises Forbidden on a 403."""
    out = {}
    for key, query in QUERIES.items():
        url = PROMQL_ROOT.format(project=project) + "?" + urllib.parse.urlencode({"query": query, "time": iso(now)})
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with opener(request, timeout=CALL_TIMEOUT_S) as response:
                out[key] = _value(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            if exc.code == FORBIDDEN:
                raise Forbidden(project) from exc
            return {"project": project, "error": f"HTTP {exc.code}"}
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            # HTTPException covers a body cut short (IncompleteRead), which is not an OSError.
            return {"project": project, "error": f"{type(exc).__name__}: {exc}"}
    return {"project": project, **out}


def lane_values(data: dict, now: datetime) -> dict:
    """The lane's nightly values over the window, each the mean of its nights'."""
    nights = [o for o in data.get("slack_overhead") or [] if isinstance(o, dict)
              and (day := parse_iso(f"{o.get('date')}T00:00:00+00:00")) and (now.date() - day.date()).days <= WINDOW.days]
    out: dict = {"nights": len(nights)}
    for key in KEYS:
        values = [o[key] for o in nights if isinstance(o.get(key), (int, float)) and not isinstance(o.get(key), bool)]
        out[key] = round(statistics.fmean(values), ROUND_TO) if values else None
    return out


def due(prior: pathlib.Path, now: datetime) -> bool:
    try:
        document = json.loads(prior.read_text(encoding="utf-8"))
        last = parse_iso(document.get("generated_at"))
    except (OSError, ValueError, AttributeError):
        return True
    if last and now - last < WINDOW and document.get("complete") is not False:
        log(f"slack crosscheck: last run {iso(last)}, next due {iso(last + WINDOW)}")
        return False
    return True


def main(argv=None, runner=subprocess.run, opener=urllib.request.urlopen) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=pathlib.Path, required=True)
    parser.add_argument("--env", action="append", default=[], metavar="NAME=PROJECT")
    parser.add_argument("--prior", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    parser.add_argument("--token-command", default=DEFAULT_TOKEN_COMMAND)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    if not due(args.prior, now):
        return 0
    token = access_token(args.token_command, runner)
    environments = {}
    for name, _, project in (e.partition("=") for e in args.env):
        try:
            environments[name] = read_environment(project, now, token or "", opener)
        except Forbidden:
            log(f"slack crosscheck: skipped {name}: no permission to read Cloud Monitoring in {project} (roles/monitoring.viewer)")
            continue
        if "error" in environments[name]:
            log(f"slack crosscheck: {name} ({project}): {environments[name]['error']}")
    if not any("error" not in e for e in environments.values()):
        log("slack crosscheck: no environment read; nothing written")
        return 0
    data = json.loads(args.data.read_text(encoding="utf-8"))
    lane = lane_values(data, now)
    for env in environments.values():
        if "error" not in env:
            env["ratio"] = {k: round(env[k] / lane[k], RATIO_ROUND_TO)
                          if env.get(k) is not None and lane.get(k) else None for k in KEYS}
    document = {"schema_version": SCHEMA_VERSION, "generated_at": iso(now), "window_days": WINDOW.days,
                "lane": lane, "environments": environments,
                "complete": len(environments) == len(args.env) and all("error" not in e for e in environments.values()),
                "note": "the lane's quantiles are the mean of its nightly quantiles, not a pooled quantile"}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    log(f"slack crosscheck: lane {json.dumps(lane)}; " + "; ".join(f"{n} {json.dumps(e)}" for n, e in environments.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
