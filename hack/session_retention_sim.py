#!/usr/bin/env python3
"""Simulate a profile's session store under daily retention, with Hermes' real SessionDB.

Each simulated day adds `--cards-per-day` ended kanban sessions, each with
`--messages-per-card` messages totalling about `--bytes-per-card` of text. The
script then runs Hermes' own prune (`SessionDB.prune_sessions`, the call
`maybe_auto_prune_and_vacuum` makes) with the cut-off set to `--retention-days`
before the end of that day. It never VACUUMs, matching the session-retention job, unless `--vacuum-over` asks it
to model the option of compacting after a prune.
It runs for `--days` and records the store's bytes on disk each day: `state.db`,
its `-wal`, and the transcript directory. The default is 4 x retention + 30. Rows
level off as soon as the first prune runs, but the file doesn't: SQLite reuses
freed pages only partly (index pages fragment, FTS5 keeps segments until it
merges them), so the file keeps growing until it settles at a multiple of the
live data. In measured runs that took 3.5-4 x the retention period.

Time is simulated by back-dating rows after Hermes writes them, so the run takes
minutes and the prune selects exactly what it would have on each day.

The ceiling is derived, not chosen:

    window  = retention_days * cards_per_day * stored_bytes_per_card
    ceiling = overhead + window * slack

- `overhead` is an empty store's size, measured by this script.
- `stored_bytes_per_card` is the measured growth per card in the run's first
  days, before any prune. It includes SQLite's page and index overhead (FTS
  included), not just the text.
- `slack` (default 4.0) covers the file's settled multiple of its live data. The
  run reports that multiple as `plateau_factor`. It rises with volume: 2.2-2.5 at
  5 cards a day, about 3.5 at 25.

The verdict is that the plateau (the largest size in the run's last 30 days,
reported with the spread over those 30 days, which is near 0 once it has settled) stays under the ceiling. Logs are reported separately at Hermes' cap: 4 x 5 MiB per log
name (a live file plus 3 rotated backups).

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
WORDS = (
    "cluster node pod deployment namespace service ingress replica rollout image tag "
    "quota limit request memory cpu disk volume claim storage class event warning error "
    "restart backoff probe readiness liveness container sidecar policy role binding "
    "account secret config map label selector annotation upgrade version channel "
    "kubectl describe get logs status ready pending running failed succeeded evicted"
).split()


def store_bytes(home: Path) -> dict:
    db = home / "state.db"
    sizes = {
        "db": db.stat().st_size if db.exists() else 0,
        "wal": (home / "state.db-wal").stat().st_size if (home / "state.db-wal").exists() else 0,
        "transcripts": sum(p.stat().st_size for p in (home / "sessions").glob("*") if p.is_file())
        if (home / "sessions").is_dir()
        else 0,
    }
    sizes["total"] = sizes["db"] + sizes["wal"] + sizes["transcripts"]
    return sizes


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
        (home / "sessions").mkdir()
        db = SessionDB(db_path=home / "state.db")
        overhead = store_bytes(home)["total"]
        journal = db._conn.execute("PRAGMA journal_mode").fetchone()[0] if hasattr(db, "_conn") else "unknown"
        raw = sqlite3.connect(home / "state.db", timeout=30, isolation_level=None)
        days = args.days or 4 * args.retention_days + 30
        start = time.time() - days * DAY
        per_message = max(1, args.bytes_per_card // args.messages_per_card)
        series, pruned_total, n, vacuums = [], 0, 0, 0
        for day in range(days):
            ids = []
            for _ in range(args.cards_per_day):
                n += 1
                sid = f"sim-{n:07d}"
                db.create_session(sid, "kanban")
                for i in range(args.messages_per_card):
                    db.append_message(sid, "assistant" if i % 2 else "user", content=text_of(rng, per_message))
                db.end_session(sid, "completed")
                ids.append(sid)
            stamp = start + day * DAY + DAY / 2
            raw.execute("BEGIN IMMEDIATE")
            marks = ",".join("?" * len(ids))
            raw.execute(
                f"UPDATE sessions SET started_at=?, ended_at=?, last_activity_at=? WHERE id IN ({marks})",
                (stamp, stamp, stamp, *ids),
            )
            raw.execute(f"UPDATE messages SET timestamp=? WHERE session_id IN ({marks})", (stamp, *ids))
            raw.execute("COMMIT")
            end_of_day = start + (day + 1) * DAY
            pruned = db.prune_sessions(
                older_than_days=None,
                sessions_dir=home / "sessions",
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
            sizes = store_bytes(home)
            live = raw.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            series.append({"day": day + 1, "pruned": pruned, "sessions": live, **sizes})
        raw.close()
        db.close()
    finally:
        shutil.rmtree(home, ignore_errors=True)

    warmup = min(args.warmup_days, args.retention_days) or 1
    stored_per_card = (series[warmup - 1]["total"] - overhead) / (warmup * args.cards_per_day)
    window = args.retention_days * args.cards_per_day * stored_per_card
    ceiling = overhead + window * args.slack
    tail = [s["total"] for s in series[-30:]] if len(series) > args.retention_days + 30 else []
    plateau = max(tail) if tail else None
    spread = round((max(tail) - min(tail)) / max(tail), 3) if tail else None
    day30 = next((s["total"] for s in series if s["day"] == 30), None)
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
        "plateau_factor": None if plateau is None else round((plateau - overhead) / window, 2),
        "pruned_total": pruned_total,
        "vacuums": vacuums,
        "logs_cap_bytes_per_log_name": LOG_CAP_BYTES_PER_NAME,
        "verdict": None if plateau is None else ("under the ceiling" if plateau <= ceiling else "OVER the ceiling"),
        "series": series if args.series else None,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--cards-per-day", type=int, required=True)
    parser.add_argument("--bytes-per-card", type=int, required=True, help="text bytes per card's transcript")
    parser.add_argument("--messages-per-card", type=int, default=20)
    parser.add_argument("--days", type=int, help="default: 4 x retention + 30")
    parser.add_argument("--warmup-days", type=int, default=5, help="days used to measure stored bytes per card")
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
    return 0 if result["verdict"] in (None, "under the ceiling") else 1


if __name__ == "__main__":
    sys.exit(main())
