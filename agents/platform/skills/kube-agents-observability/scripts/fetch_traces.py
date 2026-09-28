#!/usr/bin/env python3
"""List recent Cloud Trace traces, read through the credential broker's relay."""

from __future__ import annotations

import argparse
import json
import sys

import google_api

PAGE_SIZE = 10


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch traces from Cloud Trace API")
    parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
    parser.add_argument(
        "--hours", type=int, default=google_api.DEFAULT_WINDOW_HOURS,
        help=f"Retrieve traces for the last N hours (default: {google_api.DEFAULT_WINDOW_HOURS})",
    )
    return parser.parse_args(argv)


def main(argv=None, session=None) -> int:
    args = parse_args(argv)
    try:
        session = session or google_api.open_session()
        data = google_api.list_traces(session, args.project_id, args.hours, PAGE_SIZE)
    except google_api.RelayError as exc:
        print(f"Error querying the Trace API: {exc}", file=sys.stderr)
        return google_api.EXIT_READ_FAILED
    print(json.dumps(data, indent=google_api.JSON_INDENT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
