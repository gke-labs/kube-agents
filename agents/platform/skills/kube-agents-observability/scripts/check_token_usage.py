#!/usr/bin/env python3
"""Sum the LiteLLM token counters over the last day, read through the credential broker's relay."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

import google_api

TIME_SERIES_URL = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
INPUT_TOKENS_METRIC = "prometheus.googleapis.com/litellm_input_tokens_metric_total/counter"
OUTPUT_TOKENS_METRIC = "prometheus.googleapis.com/litellm_output_tokens_metric_total/counter"
CACHED_INPUT_TOKENS_METRIC = "prometheus.googleapis.com/litellm_input_cached_tokens_metric_total/counter"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query token usage delta for GKE Managed Service for Prometheus metrics"
    )
    parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
    return parser.parse_args(argv)


def parse_value(pt) -> float:
    """A point's value; int64Value arrives as a string in the REST API."""
    val_obj = pt.get("value") or {}
    val = val_obj.get("doubleValue")
    if val is None:
        try:
            val = int(val_obj.get("int64Value", "0"))
        except ValueError:
            val = 0
    return val


def counter_delta(data: dict) -> float:
    """The summed increase across every series, counter resets counted from zero."""
    total_delta = 0
    for ts in data.get("timeSeries", []):
        points = ts.get("points", [])
        if len(points) < 2:
            continue
        points.sort(key=lambda x: (x.get("interval") or {}).get("endTime", ""))
        prev_val = None
        for pt in points:
            val = parse_value(pt)
            if prev_val is not None:
                diff = val - prev_val
                total_delta += diff if diff >= 0 else val
            prev_val = val
    return total_delta


def get_token_delta(session, project_id: str, metric_name: str, start_str: str, end_str: str) -> float:
    params = {
        "filter": f'metric.type="{metric_name}"',
        "interval.startTime": start_str,
        "interval.endTime": end_str,
    }
    url = TIME_SERIES_URL.format(project=urllib.parse.quote(project_id, safe=""))
    return counter_delta(google_api.get_json(session, url, params=params))


def main(argv=None, session=None) -> int:
    args = parse_args(argv)
    start_str, end_str = google_api.window(google_api.DEFAULT_WINDOW_HOURS)
    try:
        session = session or google_api.open_session()
        usage = {
            "input_tokens": get_token_delta(session, args.project_id, INPUT_TOKENS_METRIC, start_str, end_str),
            "output_tokens": get_token_delta(session, args.project_id, OUTPUT_TOKENS_METRIC, start_str, end_str),
            "cached_input_tokens": get_token_delta(
                session, args.project_id, CACHED_INPUT_TOKENS_METRIC, start_str, end_str
            ),
        }
    except google_api.RelayError as exc:
        print(f"Error querying the Monitoring API: {exc}", file=sys.stderr)
        return google_api.EXIT_READ_FAILED
    print(json.dumps(usage, indent=google_api.JSON_INDENT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
