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
# Filtered on the server: a project's whole descriptor list runs past the
# broker relay's response cap (observed on a live install as a 502 naming the
# 8 MiB limit), and paged, since the filtered list can still be long.
DESCRIPTOR_FILTER = f'metric.type = has_substring("{METRIC_NAME_SUBSTRING}")'
PAGE_SIZE = 1000


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=f"List available metric descriptors matching '{METRIC_NAME_SUBSTRING}'"
    )
    parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
    return parser.parse_args(argv)


def list_descriptors(session, project_id: str) -> list:
    url = METRIC_DESCRIPTORS_URL.format(project=urllib.parse.quote(project_id, safe=""))
    params = {"filter": DESCRIPTOR_FILTER, "pageSize": PAGE_SIZE}
    return google_api.get_paginated(session, url, params=params, items_key="metricDescriptors")


def main(argv=None, session=None) -> int:
    args = parse_args(argv)
    try:
        session = session or google_api.open_session()
        descriptors = list_descriptors(session, args.project_id)
    except google_api.RelayError as exc:
        print(f"Error querying the Monitoring API: {exc}", file=sys.stderr)
        return google_api.EXIT_READ_FAILED
    matching = [m.get("type") for m in descriptors if m.get("type") and METRIC_NAME_SUBSTRING in m.get("type")]
    print(json.dumps(matching, indent=google_api.JSON_INDENT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
