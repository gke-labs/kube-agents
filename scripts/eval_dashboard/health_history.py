#!/usr/bin/env python3
"""Append one tick's health.json to the health history feed (JSON Lines).

health.json is overwritten every 15 minutes, so on its own it cannot answer
"when did this start" or "how often does the gate go red". The feed keeps
every tick: one line per run of the job, the whole health.json object plus
`tick`, the ISO 8601 UTC time the line was appended. Nothing trims it -- at
~100 lines a day it is small for years -- and nothing else writes it.

GCS has no append. The workflow downloads the object (or starts from
nothing), runs this, and uploads the result; a failure there is a warning,
never a failed tick. docs/ci-health.md documents the schema.

Run:  python3 scripts/eval_dashboard/health_history.py --health health.json --history health-history.jsonl
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import datetime, timezone

TICK_KEY = "tick"
UTC = timezone.utc


def parse_iso(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def line_for(health: dict, tick: datetime) -> str:
    """One JSON Lines record: health.json verbatim plus `tick`."""
    record = dict(health)
    record[TICK_KEY] = tick.astimezone(UTC).isoformat(timespec="seconds")
    return json.dumps(record, separators=(",", ":"), ensure_ascii=False)


def append(history: pathlib.Path, health: dict, tick: datetime) -> None:
    """Append to `history`, creating it if needed; a file that does not end
    in a newline gets one first so the record never joins the line above."""
    existing = history.read_bytes() if history.is_file() else b""
    with history.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith(b"\n"):
            handle.write("\n")
        handle.write(line_for(health, tick) + "\n")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--health", type=pathlib.Path, required=True, help="the health.json health.py wrote")
    parser.add_argument("--history", type=pathlib.Path, required=True, help="the JSON Lines file to append to (created if missing)")
    parser.add_argument("--now", help="the tick time, ISO 8601 (default: now)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        health = json.loads(args.health.read_text())
    except (OSError, ValueError) as exc:
        print(f"ERROR: {args.health}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(health, dict):
        print(f"ERROR: {args.health} is not a JSON object", file=sys.stderr)
        return 1
    append(args.history, health, parse_iso(args.now) or datetime.now(UTC))
    print(f"appended {health.get('state')} to {args.history}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
