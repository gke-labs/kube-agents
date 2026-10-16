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
# The window is summed on the server. `timeSeries.list` with an aggregation
# answers the question directly: ALIGN_DELTA turns each cumulative series
# into its increase over one alignment period the width of the window, with
# counter resets handled where the series is kept, and REDUCE_SUM adds every
# series into one, so the reply is one series with one point whatever the
# install's label cardinality. Read at the FULL view instead, the list pages
# in points, and a day of points from a few hundred series ran past any page
# cap a reader could hold; the broker forwards `aggregation.*` as it does
# every other query key, and fleet_waste.py sends the same keys today.
ALIGNMENT_PERIOD_PARAM = "aggregation.alignmentPeriod"
PER_SERIES_ALIGNER_PARAM = "aggregation.perSeriesAligner"
CROSS_SERIES_REDUCER_PARAM = "aggregation.crossSeriesReducer"
ALIGN_DELTA = "ALIGN_DELTA"
REDUCE_SUM = "REDUCE_SUM"
SECONDS_PER_HOUR = 3600


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


def aggregation_params(hours: int) -> dict:
    """The query keys that make Monitoring sum the counters' increase over the window."""
    return {
        ALIGNMENT_PERIOD_PARAM: f"{hours * SECONDS_PER_HOUR}s",
        PER_SERIES_ALIGNER_PARAM: ALIGN_DELTA,
        CROSS_SERIES_REDUCER_PARAM: REDUCE_SUM,
    }


def summed_points(series: list) -> float:
    """Every point of every series returned, added.

    After the reducer there is one series with one point; an aligned delta is
    additive, so a reply that split the window or the series into more than
    one still sums to the increase over the window.
    """
    return sum(parse_value(pt) for ts in series for pt in ts.get("points", []))


def get_token_delta(session, project_id: str, metric_name: str, start_str: str, end_str: str, hours: int) -> float:
    params = {
        "filter": f'metric.type="{metric_name}"',
        "interval.startTime": start_str,
        "interval.endTime": end_str,
        **aggregation_params(hours),
    }
    url = TIME_SERIES_URL.format(project=urllib.parse.quote(project_id, safe=""))
    return summed_points(google_api.get_paginated(session, url, params=params, items_key="timeSeries", whole=True))


def main(argv=None, session=None) -> int:
    args = parse_args(argv)
    hours = google_api.DEFAULT_WINDOW_HOURS
    start_str, end_str = google_api.window(hours)
    try:
        session = session or google_api.open_session()
        usage = {
            metric_key: get_token_delta(session, args.project_id, metric, start_str, end_str, hours)
            for metric_key, metric in (
                ("input_tokens", INPUT_TOKENS_METRIC),
                ("output_tokens", OUTPUT_TOKENS_METRIC),
                ("cached_input_tokens", CACHED_INPUT_TOKENS_METRIC),
            )
        }
    except google_api.RelayError as exc:
        print(f"Error querying the Monitoring API: {exc}", file=sys.stderr)
        return google_api.EXIT_READ_FAILED
    print(json.dumps(usage, indent=google_api.JSON_INDENT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
