#!/usr/bin/env python3
"""Count the users who reached the agent over chat, from Cloud Logging.

Logging's `entries:list` is a POST the credential broker's relay does not
carry, so this helper reads through `gcloud logging read` instead: in the
sandbox `gcloud` is the broker's shim and that command is on its read
allowlist, so the read runs on the broker's identity and no token is fetched
here. `--freshness` is passed because gcloud's default is one day, which would
silently truncate a wider window.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# The constants below that the relay helpers share through google_api.py are
# declared again here on purpose: this helper reads through the gcloud shim,
# never the relay, and importing the relay module would say otherwise.
GCLOUD = "gcloud"
LOG_FILTER = 'resource.type="k8s_container" "Logging incoming GChat event"'
LOG_LIMIT = 1000
LOG_FORMAT = "json"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_HOURS = 24
# `logging read` returns within a minute or two; the shim's own connect
# timeout is separate.
GCLOUD_TIMEOUT_SECONDS = 300
EXIT_READ_FAILED = 1
JSON_INDENT = 2
EMAIL_PATTERN = re.compile(r"User=([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})")


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List users and message counts who interacted with the system via chat in the last 24 hours"
    )
    parser.add_argument("--project-id", required=True, help="Google Cloud Project ID")
    parser.add_argument(
        "--hours", type=int, default=DEFAULT_HOURS, help=f"Time window in hours (default: {DEFAULT_HOURS})"
    )
    return parser.parse_args(argv)


def logging_read_argv(project_id: str, hours: int) -> list[str]:
    """The brokered read: every flag here is on the shim's allowlist."""
    return [
        GCLOUD,
        "logging",
        "read",
        LOG_FILTER,
        f"--project={project_id}",
        f"--limit={LOG_LIMIT}",
        f"--format={LOG_FORMAT}",
        f"--freshness={hours}h",
    ]


def read_entries(project_id: str, hours: int) -> list:
    argv = logging_read_argv(project_id, hours)
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=GCLOUD_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{GCLOUD} was not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gcloud logging read did not finish in {GCLOUD_TIMEOUT_SECONDS}s") from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"gcloud logging read exited {completed.returncode}: {completed.stderr.strip()}"
        )
    try:
        entries = json.loads(completed.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError(f"gcloud logging read did not return JSON: {exc}") from exc
    return entries if isinstance(entries, list) else []


def entry_text(entry: dict) -> str:
    text = entry.get("textPayload", "")
    json_payload = entry.get("jsonPayload")
    if not text and json_payload:
        if isinstance(json_payload, dict):
            text = json_payload.get("log", "") or json.dumps(json_payload)
        else:
            text = str(json_payload)
    return text if isinstance(text, str) else str(text)


def count_users(entries: list) -> dict[str, int]:
    user_counts: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        match = EMAIL_PATTERN.search(entry_text(entry))
        if match:
            email = match.group(1)
            user_counts[email] = user_counts.get(email, 0) + 1
    return {k: user_counts[k] for k in sorted(user_counts)}


def main(argv=None) -> int:
    args = parse_args(argv)
    start_str = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).strftime(TIMESTAMP_FORMAT)
    try:
        entries = read_entries(args.project_id, args.hours)
    except RuntimeError as exc:
        print(f"Error querying Cloud Logging: {exc}", file=sys.stderr)
        return EXIT_READ_FAILED
    print(
        json.dumps(
            {
                "active_chat_users": count_users(entries),
                "time_window_hours": args.hours,
                "query_start_time": start_str,
            },
            indent=JSON_INDENT,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
