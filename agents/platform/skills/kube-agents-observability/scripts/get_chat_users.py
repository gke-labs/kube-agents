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
# The most entries one read asks for, newest first, and the ceiling of the
# `--limit` flag. The flag is the knob that always shrinks a body the broker's
# stdout cap cut: the body is the newest entries whatever the window, so
# `--hours` shrinks it only once the narrower window holds fewer than the
# limit.
LOG_LIMIT = 1000
LIMIT_FLOOR = 1
LOG_FORMAT = "json"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_HOURS = 24
# `logging read` returns within a minute or two. The broker runs the command
# under its own deadline (CREDENTIAL_PROXY_TIMEOUT_SECONDS, 300 s by default,
# in credential_proxy.py) measured from when the command starts, after up to
# COMMAND_SLOT_WAIT_SECONDS (60 s) queued for a slot, while this clock starts
# before the shim has connected. This value sits above both together at the
# broker's defaults, so the broker's answer, the output or its own timeout
# notice, arrives here rather than the shim being killed first with no
# stderr from either side; the test module reads both numbers from the
# broker's source and holds the ordering. The deadline is a knob the broker
# reads at startup and the sandbox cannot see, so on a broker run with a
# longer one this limit fires first, and the message below says only what
# is known here: the helper's own limit passed.
GCLOUD_TIMEOUT_SECONDS = 420
# The broker caps a relayed command's stdout; past the cap the shim writes the
# cut body, prints this line on stderr and exits as the command did. The cut
# body is not the read, so the line is a failure here whatever the exit code.
# Same words as credential_proxy_client.py prints; the test holds them equal.
SHIM_TRUNCATION_NOTE = "credential proxy output truncated"
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
    parser.add_argument(
        "--limit",
        type=int,
        default=LOG_LIMIT,
        help=(
            f"Most entries to read, newest first, {LIMIT_FLOOR} to {LOG_LIMIT} (default: {LOG_LIMIT}); "
            f"lower it when the read is cut by the credential broker's output cap"
        ),
    )
    args = parser.parse_args(argv)
    if not LIMIT_FLOOR <= args.limit <= LOG_LIMIT:
        parser.error(f"--limit must be between {LIMIT_FLOOR} and {LOG_LIMIT}, got {args.limit}")
    return args


def logging_read_argv(project_id: str, hours: int, limit: int = LOG_LIMIT) -> list[str]:
    """The brokered read: every flag here is on the shim's allowlist."""
    return [
        GCLOUD,
        "logging",
        "read",
        LOG_FILTER,
        f"--project={project_id}",
        f"--limit={limit}",
        f"--format={LOG_FORMAT}",
        f"--freshness={hours}h",
    ]


def read_entries(project_id: str, hours: int, limit: int = LOG_LIMIT) -> list:
    argv = logging_read_argv(project_id, hours, limit)
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=GCLOUD_TIMEOUT_SECONDS, check=False
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{GCLOUD} was not found on PATH: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"gcloud logging read did not return within {GCLOUD_TIMEOUT_SECONDS}s, this helper's "
            f"own limit; it sits above the credential broker's default command deadline and slot "
            f"wait, so at the defaults the broker went quiet, while a broker configured with a "
            f"longer deadline may still have been working"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"gcloud logging read exited {completed.returncode}: {completed.stderr.strip()}"
        )
    if SHIM_TRUNCATION_NOTE in completed.stderr:
        raise RuntimeError(
            f"gcloud logging read returned more than the credential broker relays for one "
            f"command ({SHIM_TRUNCATION_NOTE}); the {limit} newest entries do not fit, so "
            f"lower --limit (the body is the newest entries whatever the window, so --hours "
            f"shrinks it only once the narrower window holds fewer than {limit})"
        )
    # `--format=json` answers an empty window with the literal `[]`, so an
    # empty body is one that went missing between gcloud and here, and it
    # fails below as the non-JSON it is rather than being read as no users.
    try:
        entries = json.loads(completed.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"gcloud logging read did not return JSON: {exc}; stderr: {completed.stderr.strip()}"
        ) from exc
    # `--format=json` answers a list, `[]` included. Any other shape is a read
    # this helper cannot count, not an empty window, and the count of nothing
    # is indistinguishable from a quiet day, so it fails the same way a cut or
    # non-JSON body does.
    if not isinstance(entries, list):
        raise RuntimeError(
            f"gcloud logging read did not return a JSON list but a "
            f"{type(entries).__name__}; stderr: {completed.stderr.strip()}"
        )
    return entries


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
        entries = read_entries(args.project_id, args.hours, args.limit)
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
