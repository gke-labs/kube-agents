#!/usr/bin/env python3
"""List the LiteLLM metric descriptors, read through the credential broker's relay."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

import google_api

METRIC_DESCRIPTORS_URL = "https://monitoring.googleapis.com/v3/projects/{project}/metricDescriptors"
METRIC_NAME_SUBSTRING = "litellm"
JSON_INDENT = 2


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"List available metric descriptors matching '{METRIC_NAME_SUBSTRING}'"
    )
    parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
    return parser.parse_args(argv)


def list_descriptors(session, project_id: str) -> dict:
    url = METRIC_DESCRIPTORS_URL.format(project=urllib.parse.quote(project_id, safe=""))
    return google_api.get_json(session, url)


def main(argv=None, session=None) -> int:
    args = parse_args(argv)
    try:
        session = session or google_api.open_session()
        descriptors = list_descriptors(session, args.project_id)
    except google_api.RelayError as exc:
        print(f"Error querying the Monitoring API: {exc}", file=sys.stderr)
        return google_api.EXIT_READ_FAILED
    matching = [
        m.get("type")
        for m in descriptors.get("metricDescriptors", [])
        if m.get("type") and METRIC_NAME_SUBSTRING in m.get("type")
    ]
    print(json.dumps(matching, indent=JSON_INDENT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
