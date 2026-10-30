#!/usr/bin/env python3
"""session_retention.py - run Hermes' own session pruning daily for every profile on the volume.

Hermes deletes ended sessions inactive for ``sessions.retention_days`` (their
messages, transcript files and unreferenced system prompts) whenever a CLI or the
gateway starts: ``SessionDB.maybe_auto_prune_and_vacuum``, called from ``cli.py``
and ``gateway/run.py``, at most once a day per ``state.db``. Two stores never get
that start:

- the machine home's, which only the long-lived gateway opens, at construction;
- a profile that has not run a process since its history aged out.

So their history grew without bound: a Cluster Agent profile on a quiet
dev install reached 16.5 MB, most of it ``state.db`` and its WAL.

This is a ``no_agent`` entry on the Platform Agent's roster. Each daily tick it
calls the same Hermes method on every profile's ``state.db`` with the same
period, so it inherits Hermes' guarantees rather than restating them:

- open, pinned and mid-turn sessions are never deleted;
- dead automation sessions are closed first and get a full period before removal;
- one maintenance lock and a throttle per store, so a worker starting at the same
  moment cannot double-run it (20 hours here rather than Hermes' 24, so a daily tick
  never skips a day);
- every session that still has a child is pinned first, so no prune deletes the
  origin a live continuation's ``parent_session_id`` points to (``keep_parents``);
- no VACUUM from here: the gateway and workers hold these stores while it runs, and
  Hermes VACUUMs each one when a process starts on it.

The period is the managed scope's ``sessions.retention_days``, which the operator
renders from ``spec.harness.retention.sessionDays``. A blocked kanban card needs
no exemption: its worker starts a fresh session on every run
(``hermes_cli/kanban_db_dispatch.py``, ``chat -q "work kanban task <id>"``, with
no resume) and reads its context from the card.

It also removes rotated Hermes log backups (``<name>.log.<n>``) older than the
period. The live log files keep Hermes' own size cap. ``audit.jsonl`` and its
rotations are never touched: the audit ledger has its own retention.

Prints nothing on a clean run. A store it could not maintain prints one line, so
the failure reaches chat.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gitops_workspace  # noqa: E402

PROFILES_DIR = "profiles"
STATE_DB = "state.db"
SESSIONS_DIR = "sessions"
LOGS_DIR = "logs"
# Only Hermes' rotated log backups (agent.log.1, errors.log.3, ...). Not
# audit.jsonl.<n>, not the live <name>.log, not anything else under logs/.
ROTATED_LOG_RE = re.compile(r"^[A-Za-z0-9_.-]+\.log\.[0-9]+$")
SECONDS_PER_DAY = 86400
WATCH_NAME = "Session retention"
FAILED_PREFIX = f"⚠️ **{WATCH_NAME}:**"
# The ids this job pinned, in each store's state_meta, so it only ever unpins its own.
PIN_LEDGER_KEY = "kube_agents_retention_pins"
SQLITE_TIMEOUT_SECONDS = 30
# Hermes' error text goes inside one inline code span in chat; this bounds it.
ERROR_SPAN_CHARS = 300
# Hermes skips a store pruned less than min_interval_hours ago, timed from the
# previous pass's start. At its own 24 a daily tick that starts a few seconds
# earlier than yesterday's would skip, and the deletion would slip a day.
MIN_INTERVAL_HOURS = 20


def retention_days() -> int | None:
    """The period from Hermes' merged config, where the managed scope wins."""
    try:
        from hermes_cli.config import load_config  # the managed scope is merged in here

        raw = (load_config().get("sessions") or {}).get("retention_days")
    except Exception:  # noqa: BLE001 - an unreadable config is "no usable period"
        return None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return None
    return days if days >= 1 else None


def homes(agent_home: Path) -> list[Path]:
    """The machine home, then every profile directory, sorted."""
    found = [agent_home]
    profiles = agent_home / PROFILES_DIR
    if profiles.is_dir():
        found += sorted(p for p in profiles.iterdir() if p.is_dir() and not p.name.startswith("."))
    return found


def prune_store(home: Path, days: int) -> dict:
    """Hermes' own prune on ``home``'s state.db, never a VACUUM; {} when the home has none.

    The gateway serves from the machine home or, with the Platform Agent as front
    door, from profiles/platform, and workers write to their profiles' stores
    while this runs; a VACUUM rewrites the whole file and holds every writer up
    for as long as it takes. Hermes VACUUMs each store when a process starts on
    it, and SQLite reuses the pages a prune frees, so skipping it here costs
    space that is not lost.
    """
    db_path = home / STATE_DB
    if not db_path.is_file():
        return {}
    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path)
    try:
        return db.maybe_auto_prune_and_vacuum(
            retention_days=days,
            sessions_dir=home / SESSIONS_DIR,
            min_interval_hours=MIN_INTERVAL_HOURS,
            vacuum=False,
        )
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            close()


def keep_parents(home: Path) -> None:
    """Pin every session that still has a child, and unpin the ones this job pinned that have none.

    Readers here follow ``parent_session_id`` back to where a conversation
    started (a compression continuation to its ``a2a-*`` origin, for one), and
    Hermes' prune clears that link when it deletes the parent. A parent only
    ages on its own rows, so a conversation still going long after it was
    compressed would lose its origin. Hermes' prune skips pinned rows, so a pin
    keeps the parent for as long as a child exists, and every prune honours it,
    the ones Hermes runs at process start included. When the child goes, the
    parent is unpinned on the next tick and ages out like any other session.

    It sets the flag on the one row, not through ``set_session_pinned``, which
    pins the whole lineage and unhides it. The ledger keeps it to its own pins:
    a pin someone set by hand is never cleared here.
    """
    db_path = home / STATE_DB
    if not db_path.is_file():
        return
    conn = sqlite3.connect(db_path, timeout=SQLITE_TIMEOUT_SECONDS, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (PIN_LEDGER_KEY,)).fetchone()
        ledger = set(json.loads(row[0])) if row and row[0] else set()
        parents = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT p.id FROM sessions p JOIN sessions c ON c.parent_session_id = p.id"
            )
        }
        unpinned = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT p.id FROM sessions p JOIN sessions c ON c.parent_session_id = p.id"
                " WHERE COALESCE(p.pinned, 0) = 0"
            )
        }
        present = {r[0] for r in conn.execute("SELECT id FROM sessions") if r[0] in ledger}
        release = (ledger & present) - parents
        conn.executemany("UPDATE sessions SET pinned = 1 WHERE id = ?", [(i,) for i in sorted(unpinned)])
        conn.executemany("UPDATE sessions SET pinned = 0 WHERE id = ?", [(i,) for i in sorted(release)])
        ledger = ((ledger & present) - release) | unpinned
        conn.execute(
            "INSERT INTO state_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (PIN_LEDGER_KEY, json.dumps(sorted(ledger))),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def remove_old_rotated_logs(home: Path, days: int, now: float) -> int:
    """Delete ``<name>.log.<n>`` under home/logs older than ``days``; return how many went."""
    logs = home / LOGS_DIR
    if not logs.is_dir():
        return 0
    cutoff = now - days * SECONDS_PER_DAY
    removed = 0
    for entry in logs.iterdir():
        if not ROTATED_LOG_RE.match(entry.name) or not entry.is_file() or entry.is_symlink():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except FileNotFoundError:
            continue  # Hermes rotated it away under the scan
    return removed


def label(home: Path, agent_home: Path) -> str:
    return "the machine home" if home == agent_home else f"profile `{home.name}`"


def span_safe(text: object) -> str:
    """One line, no backticks, bounded, so it stays inside its code span."""
    return " ".join(str(text).split()).replace("`", "'")[:ERROR_SPAN_CHARS]


def tick(agent_home: Path, *, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    days = retention_days()
    if days is None:
        return [f"{FAILED_PREFIX} no usable `sessions.retention_days` in the config; nothing was pruned."]
    lines: list[str] = []
    for home in homes(agent_home):
        # Separate tries: one store's failure must not stop the rest, and a log
        # that cannot be removed is not a failed prune.
        try:
            keep_parents(home)  # before the prune; if it fails, the store is not pruned
            result = prune_store(home, days)
            if result.get("error"):
                lines.append(f"{FAILED_PREFIX} pruning {label(home, agent_home)} failed: `{span_safe(result['error'])}`")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{FAILED_PREFIX} pruning {label(home, agent_home)} failed: `{type(exc).__name__}`")
        try:
            remove_old_rotated_logs(home, days, now)
        except Exception as exc:  # noqa: BLE001
            lines.append(
                f"{FAILED_PREFIX} removing rotated logs for {label(home, agent_home)} failed: `{type(exc).__name__}`"
            )
    return lines


def main() -> int:
    lines = tick(Path(gitops_workspace.agent_home()))
    if lines:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
