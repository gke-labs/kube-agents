#!/usr/bin/env python3
"""Rank the spans of recent traces by duration to locate a slow tool or model call.

Both Cloud Trace reads go through the credential broker's relay (google_api.py):
the list of traces in the window, then each trace's spans.
"""

from __future__ import annotations

import argparse
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import google_api

TRACE_LIST_URL = "https://cloudtrace.googleapis.com/v1/projects/{project}/traces"
TRACE_GET_URL = "https://cloudtrace.googleapis.com/v1/projects/{project}/traces/{trace_id}"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_HOURS = 24
DEFAULT_LIMIT = 5
# How many of a trace's slowest spans the breakdown lists.
TOP_SPANS = 10
# fromisoformat reads at most microseconds; Cloud Trace writes nanoseconds.
FRACTION_DIGITS = 6
RULE_WIDTH = 70
SPAN_NAME_WIDTH = 50


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze trace latencies to locate bottlenecks (e.g. slow tool or model calls)"
    )
    parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
    parser.add_argument(
        "--hours", type=int, default=DEFAULT_HOURS,
        help=f"Analyze traces within the last N hours (default: {DEFAULT_HOURS})",
    )
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT,
        help=f"Number of traces to fetch and analyze (default: {DEFAULT_LIMIT})",
    )
    return parser.parse_args(argv)


def parse_timestamp(ts_str):
    """A Cloud Trace timestamp, nanosecond precision and offset spellings included."""
    if not ts_str:
        return datetime.now(timezone.utc)
    ts_str = ts_str.replace("Z", "+00:00")
    if "." in ts_str:
        base, fraction_tz = ts_str.split(".", 1)
        tz_idx = -1
        for i, char in enumerate(fraction_tz):
            if char in ("+", "-"):
                tz_idx = i
                break
        if tz_idx != -1:
            fraction = fraction_tz[:tz_idx][:FRACTION_DIGITS]
            ts_str = f"{base}.{fraction}{fraction_tz[tz_idx:]}"
        else:
            ts_str = f"{base}.{fraction_tz[:FRACTION_DIGITS]}"
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        try:
            return datetime.strptime(ts_str.split(".", 1)[0], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            return datetime.now(timezone.utc)


def list_traces(session, project_id: str, hours: int, limit: int) -> dict:
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)
    params = {
        "startTime": start_time.strftime(TIMESTAMP_FORMAT),
        "endTime": end_time.strftime(TIMESTAMP_FORMAT),
        "pageSize": limit,
    }
    url = TRACE_LIST_URL.format(project=urllib.parse.quote(project_id, safe=""))
    return google_api.get_json(session, url, params=params)


def get_trace(session, project_id: str, trace_id: str) -> dict:
    url = TRACE_GET_URL.format(
        project=urllib.parse.quote(project_id, safe=""),
        trace_id=urllib.parse.quote(trace_id, safe=""),
    )
    return google_api.get_json(session, url)


def print_breakdown(trace_id: str, spans: list) -> None:
    print("=" * RULE_WIDTH)
    print(f"Trace ID: {trace_id}")
    trace_start = None
    trace_end = None
    span_durations = []
    for span in spans:
        start_t_str = span.get("startTime")
        end_t_str = span.get("endTime")
        if not start_t_str or not end_t_str:
            continue
        start_t = parse_timestamp(start_t_str)
        end_t = parse_timestamp(end_t_str)
        span_durations.append((span.get("name", "unknown"), (end_t - start_t).total_seconds()))
        if trace_start is None or start_t < trace_start:
            trace_start = start_t
        if trace_end is None or end_t > trace_end:
            trace_end = end_t
    total_duration = (trace_end - trace_start).total_seconds() if trace_start and trace_end else 0
    print(f"Total Duration: {total_duration:.3f} seconds | Total Spans: {len(spans)}")
    print("Breakdown of spans:")
    span_durations.sort(key=lambda x: x[1], reverse=True)
    for name, dur in span_durations[:TOP_SPANS]:
        pct = (dur / total_duration) * 100 if total_duration > 0 else 0
        print(f"  - {name:{SPAN_NAME_WIDTH}} : {dur:6.3f}s ({pct:4.1f}%)")
    if len(span_durations) > TOP_SPANS:
        print(f"  ... and {len(span_durations) - TOP_SPANS} more spans.")


def main(argv=None, session=None) -> int:
    args = parse_args(argv)
    print(f"Retrieving the last {args.limit} traces...")
    try:
        session = session or google_api.open_session()
        listing = list_traces(session, args.project_id, args.hours, args.limit)
    except google_api.RelayError as exc:
        print(f"Error listing traces: {exc}", file=sys.stderr)
        return google_api.EXIT_READ_FAILED
    traces = listing.get("traces") or []
    if not traces:
        print("No traces found in the specified window.")
        return 0
    for trace in traces:
        trace_id = trace.get("traceId")
        if not trace_id:
            continue
        try:
            detail = get_trace(session, args.project_id, trace_id)
        except google_api.RelayError as exc:
            print(f"Error reading trace {trace_id}: {exc}", file=sys.stderr)
            continue
        spans = detail.get("spans") or []
        if spans:
            print_breakdown(trace_id, spans)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
