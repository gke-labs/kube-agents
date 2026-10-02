#!/usr/bin/env python3
"""Rank the spans of recent traces by duration to locate a slow tool or model call.

Both Cloud Trace reads go through the credential broker's relay (google_api.py):
the list of traces in the window, then each trace's spans.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import google_api

DEFAULT_LIMIT = 5
# How many of a trace's slowest spans the breakdown lists.
TOP_SPANS = 10
# fromisoformat reads at most microseconds; Cloud Trace writes nanoseconds.
FRACTION_DIGITS = 6
# The breakdown's columns: the rule above each trace, the span name, the
# duration in seconds and its share of the trace.
RULE_WIDTH = 70
SPAN_NAME_WIDTH = 50
DURATION_FORMAT = "6.3f"
PERCENT_FORMAT = "4.1f"


def positive_limit(value: str) -> int:
    """The --limit type: an int of at least google_api.MIN_LIMIT.

    Cloud Trace reads a pageSize of 0 or less as "choose a default" and
    answers a full page, which the stop rule would trim to nothing and the
    helper would report as an empty window; so the parser refuses it.
    """
    number = int(value)
    if number < google_api.MIN_LIMIT:
        raise argparse.ArgumentTypeError(f"must be at least {google_api.MIN_LIMIT}, got {value}")
    return number


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze trace latencies to locate bottlenecks (e.g. slow tool or model calls)"
    )
    parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
    parser.add_argument(
        "--hours", type=int, default=google_api.DEFAULT_WINDOW_HOURS,
        help=f"Analyze traces within the last N hours (default: {google_api.DEFAULT_WINDOW_HOURS})",
    )
    parser.add_argument(
        "--limit", type=positive_limit, default=DEFAULT_LIMIT,
        help=f"Number of traces to fetch and analyze, at least {google_api.MIN_LIMIT} (default: {DEFAULT_LIMIT})",
    )
    return parser.parse_args(argv)


def parse_timestamp(ts_str):
    """A Cloud Trace timestamp, nanosecond precision and offset spellings included.

    None for anything that is not a non-empty string: the API types the field
    as an RFC 3339 string, and a span carrying something else is skipped by
    the caller rather than crashing the breakdown part-way through.
    """
    if not isinstance(ts_str, str) or not ts_str:
        return None
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


def print_breakdown(trace_id: str, spans: list) -> None:
    print("=" * RULE_WIDTH)
    print(f"Trace ID: {trace_id}")
    trace_start = None
    trace_end = None
    span_durations = []
    for span in spans:
        start_t = parse_timestamp(span.get("startTime"))
        end_t = parse_timestamp(span.get("endTime"))
        if start_t is None or end_t is None:
            continue
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
        print(f"  - {name:{SPAN_NAME_WIDTH}} : {dur:{DURATION_FORMAT}}s ({pct:{PERCENT_FORMAT}}%)")
    if len(span_durations) > TOP_SPANS:
        print(f"  ... and {len(span_durations) - TOP_SPANS} more spans.")


def main(argv=None, session=None) -> int:
    args = parse_args(argv)
    print(f"Retrieving the last {args.limit} traces...")
    try:
        session = session or google_api.open_session()
        listing = google_api.list_traces(session, args.project_id, args.hours, args.limit)
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
            detail = google_api.get_trace(session, args.project_id, trace_id)
        except google_api.RelayError as exc:
            print(f"Error reading trace {trace_id}: {exc}", file=sys.stderr)
            continue
        spans = detail.get("spans") or []
        if spans:
            print_breakdown(trace_id, spans)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
