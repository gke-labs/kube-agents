#!/usr/bin/env python3
"""Simulate a profile's session store under daily retention, with Hermes' real SessionDB.

Each simulated day adds `--cards-per-day` ended kanban sessions, each with
`--messages-per-card` messages totalling about `--bytes-per-card` of text. The
script then runs Hermes' own prune (`SessionDB.prune_sessions`, the call
`maybe_auto_prune_and_vacuum` makes) with the cut-off `--retention-days` before
the end of that day. Like the session-retention job, it never VACUUMs, unless
`--vacuum-over` models the option of compacting after a prune.

Time is simulated: messages are written with Hermes' own `timestamp` keyword and
each session's start and end are set to the same day. Hermes' prune ages a
session by its newest message, so it selects exactly what it would have on that
day, and a run of hundreds of days takes minutes.

Each day it records the database's logical size (its file once the WAL is
checkpointed), the part of that in use, the freelist ratio and the WAL file. The
WAL is reported apart: Hermes caps it with `journal_size_limit`, so it is a fixed
term, not a share of each card. Transcript files under `sessions/` are not
modelled; the simulated cards write none.

Rows level off at the first prune, but the file doesn't: SQLite reuses freed
pages only partly, so it keeps growing past the retention period and settles,
in steps, at a multiple of its live data. The default run is 4 x retention + 30
days so that the settling shows.

The ceiling is derived, not chosen:

    window  = retention_days * cards_per_day * stored_bytes_per_card
    ceiling = overhead + window * slack

- `overhead` is an empty store's logical size.
- `stored_bytes_per_card` is the in-use bytes added per card in the run's first
  days, before any prune: the text plus SQLite's page and index overhead.
- `slack` (default 4.0) covers the settled multiple, which each run reports as
  `plateau_factor`. #3704 has the measured values.

The verdict compares the settled size (the largest logical size in the last 30
days) with the ceiling. It is "not settled" when those days vary by more than
SETTLED_SPREAD, and "too short to settle" under 4 x retention + 30 days. Logs are
reported apart at Hermes' cap: 4 x 5 MiB per log name.

Run it where Hermes is importable, e.g. inside the platform-agent image
(`/opt/hermes/.venv/bin/python3 hack/session_retention_sim.py`), or point
`--hermes-src` at a checkout of the pinned Hermes tag.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

DAY = 86400
LOG_CAP_BYTES_PER_NAME = 4 * 5 * 1024 * 1024
# The settled size is read over the run's last SETTLE_WINDOW_DAYS, which count as
# settled when they vary by no more than SETTLED_SPREAD. A default run is
# SETTLE_RETENTIONS x retention + SETTLE_WINDOW_DAYS days long.
SETTLE_WINDOW_DAYS = 30
SETTLED_SPREAD = 0.02
SETTLE_RETENTIONS = 4
# The month mark the report quotes alongside the settled size.
MONTH_DAY = 30
STATE_DB = "state.db"
SESSIONS_DIR = "sessions"
SQLITE_TIMEOUT_SECONDS = 30
WORDS = (
    "cluster node pod deployment namespace service ingress replica rollout image tag "
    "quota limit request memory cpu disk volume claim storage class event warning error "
    "restart backoff probe readiness liveness container sidecar policy role binding "
    "account secret config map label selector annotation upgrade version channel "
    "kubectl describe get logs status ready pending running failed succeeded evicted"
).split()


def store_bytes(conn: sqlite3.Connection, home: Path) -> dict:
    """The database's logical size (what its file holds once the WAL is checkpointed), the
    part of it in use, and the WAL file, which is reported apart: Hermes caps it with
    journal_size_limit, so it is a fixed term rather than a share of each card."""
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    wal = home / f"{STATE_DB}-wal"
    return {
        "total": pages * page_size,
        "live": (pages - free) * page_size,
        "freelist_ratio": round(free / pages, 3) if pages else 0.0,
        "wal": wal.stat().st_size if wal.exists() else 0,
    }


def default_days(retention_days: int) -> int:
    return SETTLE_RETENTIONS * retention_days + SETTLE_WINDOW_DAYS


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {number}")
    return number


def text_of(rng: random.Random, nbytes: int) -> str:
    out, size = [], 0
    while size < nbytes:
        word = rng.choice(WORDS)
        out.append(word)
        size += len(word) + 1
    return " ".join(out)


def run(args) -> dict:
    if args.hermes_src:
        sys.path.insert(0, str(Path(args.hermes_src).expanduser()))
    from hermes_state import SessionDB

    rng = random.Random(args.seed)
    home = Path(tempfile.mkdtemp(prefix="retention-sim-"))
    try:
        (home / SESSIONS_DIR).mkdir()
        db = SessionDB(db_path=home / STATE_DB)
        journal = db._conn.execute("PRAGMA journal_mode").fetchone()[0] if hasattr(db, "_conn") else "unknown"
        raw = sqlite3.connect(home / STATE_DB, timeout=SQLITE_TIMEOUT_SECONDS, isolation_level=None)
        overhead = store_bytes(raw, home)["total"]
        days = args.days or default_days(args.retention_days)
        wall_clock_start = time.time()
        start = wall_clock_start - days * DAY
        per_message = max(1, args.bytes_per_card // args.messages_per_card)
        series, pruned_total, n, vacuums = [], 0, 0, 0
        for day in range(days):
            stamp = start + day * DAY + DAY / 2
            for _ in range(args.cards_per_day):
                n += 1
                sid = f"sim-{n:07d}"
                db.create_session(sid, "kanban")
                for i in range(args.messages_per_card):
                    # The simulated time goes in through Hermes' own keyword: an UPDATE of
                    # messages.timestamp afterwards fires a trigger that clears each row's
                    # display identity, and the rows would no longer look like Hermes' own.
                    db.append_message(
                        sid, "assistant" if i % 2 else "user", content=text_of(rng, per_message), timestamp=stamp
                    )
                db.end_session(sid, "completed")
            # Sessions carry no such trigger. Hermes' prune ages a session by its newest
            # message, else started_at; the session columns are set to match. Today's
            # sessions are the only ones Hermes stamped with the wall clock, since every
            # earlier one has been moved into the simulated past, so a predicate picks
            # them out without binding one parameter per card.
            raw.execute(
                "UPDATE sessions SET started_at=?, ended_at=?, last_activity_at=? WHERE started_at >= ?",
                (stamp, stamp, stamp, wall_clock_start),
            )
            end_of_day = start + (day + 1) * DAY
            pruned = db.prune_sessions(
                older_than_days=None,
                sessions_dir=home / SESSIONS_DIR,
                exclude_active_write_guards=True,
                last_active_before=end_of_day - args.retention_days * DAY,
            )
            pruned_total += pruned
            if args.vacuum_over is not None and pruned:
                pages = raw.execute("PRAGMA page_count").fetchone()[0]
                free = raw.execute("PRAGMA freelist_count").fetchone()[0]
                if pages and free / pages > args.vacuum_over:
                    db.vacuum()
                    vacuums += 1
            sizes = store_bytes(raw, home)
            live = raw.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            series.append({"day": day + 1, "pruned": pruned, "sessions": live, **sizes})
        raw.close()
        db.close()
    finally:
        shutil.rmtree(home, ignore_errors=True)

    warmup = max(1, min(args.warmup_days, args.retention_days, len(series)))
    stored_per_card = (series[warmup - 1]["live"] - overhead) / (warmup * args.cards_per_day)
    window = args.retention_days * args.cards_per_day * stored_per_card
    ceiling = overhead + window * args.slack
    long_enough = len(series) >= default_days(args.retention_days)
    tail = [s["total"] for s in series[-SETTLE_WINDOW_DAYS:]] if long_enough else []
    plateau = max(tail) if tail else None
    spread = round((max(tail) - min(tail)) / max(tail), 3) if tail else None
    settled = spread is not None and spread <= SETTLED_SPREAD
    day30 = next((s["total"] for s in series if s["day"] == MONTH_DAY), None)
    if stored_per_card <= 0:
        # The warm-up's rows fit in pages the empty store already had: nothing was
        # measured, so there is no window to derive a ceiling from.
        verdict = "no per-card growth measured; raise --warmup-days or --bytes-per-card"
    elif plateau is None:
        verdict = "too short to settle"
    elif not settled:
        verdict = "not settled"
    else:
        verdict = "under the ceiling" if plateau <= ceiling else "OVER the ceiling"
    return {
        "assumptions": {
            "retention_days": args.retention_days,
            "cards_per_day": args.cards_per_day,
            "bytes_per_card": args.bytes_per_card,
            "messages_per_card": args.messages_per_card,
            "slack": args.slack,
            "days_simulated": days,
            "journal_mode": journal,
            "vacuum_over_freelist_ratio": args.vacuum_over,
        },
        "overhead_bytes": overhead,
        "stored_bytes_per_card": round(stored_per_card),
        "window_bytes": round(window),
        "ceiling_bytes": round(ceiling),
        "day30_bytes": day30,
        "plateau_bytes": plateau,
        "last_30_days_spread": spread,
        "plateau_factor": None if plateau is None or window <= 0 else round((plateau - overhead) / window, 2),
        "pruned_total": pruned_total,
        "vacuums": vacuums,
        "logs_cap_bytes_per_log_name": LOG_CAP_BYTES_PER_NAME,
        "wal_max_bytes": max(s["wal"] for s in series),
        "verdict": verdict,
        "series": series if args.series else None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--retention-days", type=positive_int, default=90)
    parser.add_argument("--cards-per-day", type=positive_int, required=True)
    parser.add_argument("--bytes-per-card", type=positive_int, required=True, help="text bytes per card's transcript")
    parser.add_argument("--messages-per-card", type=positive_int, default=20)
    parser.add_argument("--days", type=positive_int, help="default: 4 x retention + 30")
    parser.add_argument(
        "--warmup-days", type=positive_int, default=5, help="days used to measure stored bytes per card"
    )
    parser.add_argument("--slack", type=float, default=4.0)
    parser.add_argument("--seed", type=int, default=3704)
    parser.add_argument(
        "--vacuum-over",
        type=float,
        help="VACUUM after a prune when the freelist exceeds this ratio (the option, not what ships)",
    )
    parser.add_argument("--hermes-src", help="a checkout of the pinned Hermes tag, if Hermes is not importable")
    parser.add_argument("--series", action="store_true", help="include the per-day series in the output")
    parser.add_argument("--out", help="write the JSON here instead of stdout, which Hermes may also log to")
    args = parser.parse_args(argv)
    result = run(args)
    text = json.dumps(result, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 1 if result["verdict"] == "OVER the ceiling" else 0


if __name__ == "__main__":
    sys.exit(main())
