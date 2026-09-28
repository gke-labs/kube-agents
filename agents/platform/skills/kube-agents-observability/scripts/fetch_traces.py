#!/usr/bin/env python3
"""List recent Cloud Trace traces, read through the credential broker's relay."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import google_api

TRACE_LIST_URL = "https://cloudtrace.googleapis.com/v1/projects/{project}/traces"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_HOURS = 24
PAGE_SIZE = 10
JSON_INDENT = 2


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch traces from Cloud Trace API")
    parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
    parser.add_argument(
        "--hours", type=int, default=DEFAULT_HOURS,
        help=f"Retrieve traces for the last N hours (default: {DEFAULT_HOURS})",
    )
    return parser.parse_args(argv)


def list_traces(session, project_id: str, hours: int) -> dict:
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=hours)
    params = {
        "startTime": start_time.strftime(TIMESTAMP_FORMAT),
        "endTime": end_time.strftime(TIMESTAMP_FORMAT),
        "pageSize": PAGE_SIZE,
    }
    url = TRACE_LIST_URL.format(project=urllib.parse.quote(project_id, safe=""))
    return google_api.get_json(session, url, params=params)


def main(argv=None, session=None) -> int:
    args = parse_args(argv)
    try:
        session = session or google_api.open_session()
        data = list_traces(session, args.project_id, args.hours)
    except google_api.RelayError as exc:
        print(f"Error querying the Trace API: {exc}", file=sys.stderr)
        return google_api.EXIT_READ_FAILED
    print(json.dumps(data, indent=JSON_INDENT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
