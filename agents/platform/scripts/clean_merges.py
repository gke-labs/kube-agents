"""The install's track record: clean merges of each kind of fix, read from the post-merge records.

The post-merge check (designed in gke-labs/kube-agents#5054) keeps one
row per fix pull request in Session KV's ``post_merge_records`` table. This
module only reads that table; the writers own its schema. A merge is clean
when all of these hold for its row:

- ``outcome`` is ``verified_fixed``: the check saw the fix applied, the
  workload healthy and the finding gone;
- ``human_edited`` is ``0``: no commit but the agent's and no change request
  before the merge. A row that has not recorded it yet does not count;
- ``reverted_at`` is empty, or more than :data:`REVERT_WINDOW` after
  ``merged_at``.

DECIDED (alternative: count only merges whose window has closed): a clean merge
counts as soon as it is verified, and stops counting if it is reverted inside
the window; stepping down a kind that was already offered is the revert path's
job. That keeps the loop testable in days rather than weeks. ``settled`` is the
subset whose window has already closed at ``now``, for a reader that wants to
wait it out.

A kind is ``ready`` with :data:`CLEAN_MERGES_NEEDED` clean merges on at least
:data:`DAYS_NEEDED` different UTC days, so a single burst does not earn it.
Records are per install, so the count is too.

``session_kv_server.py`` serves :func:`track_record` as
``GET /v1/post-merge/clean-merges``. No HTTP here. It reads and never writes.
The Slack autonomy dial (``deploy/docker/patches/slack_dial_home.py``, behind
``KUBEAGENTS_DIAL``) shows each kind's count from that route.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

RECORDS_TABLE = "post_merge_records"
VERIFIED_FIXED = "verified_fixed"
REVERT_WINDOW = timedelta(days=7)
CLEAN_MERGES_NEEDED = 5
DAYS_NEEDED = 2

# The columns this reader needs. A table without one of them cannot say which
# merges are clean, so it is an error rather than a count of zero.
REQUIRED_COLUMNS = ("kind", "outcome", "merged_at", "human_edited", "reverted_at")


class SchemaIncomplete(RuntimeError):
    """The records table exists but lacks a column the count needs."""


def _parse_time(value: Any) -> datetime | None:
    """An ISO 8601 timestamp as an aware UTC datetime; None when absent or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_clean(row: dict[str, Any], now: datetime) -> bool:
    """Whether one record is a clean merge at ``now``."""
    if row.get("outcome") != VERIFIED_FIXED:
        return False
    if row.get("human_edited") not in (0, "0", False):
        return False
    merged = _parse_time(row.get("merged_at"))
    if merged is None or merged > now:
        return False
    reverted = row.get("reverted_at")
    if reverted in (None, ""):
        return True
    reverted_at = _parse_time(reverted)
    # A revert time nobody can read is still a revert, and so is one stamped
    # before the merge (a revert commit's author date can predate it).
    return reverted_at is not None and reverted_at - merged > REVERT_WINDOW


def count(rows: list[dict[str, Any]], now: datetime) -> dict[str, dict[str, Any]]:
    """``{kind: {clean, days, settled, ready}}`` for every kind with at least one record."""
    kinds: dict[str, dict[str, Any]] = {}
    days: dict[str, set] = {}
    for row in rows:
        kind = row.get("kind")
        if not isinstance(kind, str) or not kind:
            continue
        entry = kinds.setdefault(kind, {"clean": 0, "days": 0, "settled": 0, "ready": False})
        days.setdefault(kind, set())
        if not is_clean(row, now):
            continue
        merged = _parse_time(row["merged_at"])
        entry["clean"] += 1
        days[kind].add(merged.date())
        if now - merged > REVERT_WINDOW:
            entry["settled"] += 1
    for kind, entry in kinds.items():
        entry["days"] = len(days[kind])
        entry["ready"] = entry["clean"] >= CLEAN_MERGES_NEEDED and entry["days"] >= DAYS_NEEDED
    return kinds


def read_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every record's counted columns; an empty list when the table does not exist yet.

    Raises :class:`SchemaIncomplete` when the table lacks a :data:`REQUIRED_COLUMNS` column.
    """
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({RECORDS_TABLE})")}
    if not present:
        return []
    missing = [column for column in REQUIRED_COLUMNS if column not in present]
    if missing:
        raise SchemaIncomplete(f"{RECORDS_TABLE} has no column {', '.join(missing)}")
    cursor = conn.execute(f"SELECT {', '.join(REQUIRED_COLUMNS)} FROM {RECORDS_TABLE}")
    return [dict(zip(REQUIRED_COLUMNS, values)) for values in cursor.fetchall()]


def track_record(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """The install's clean merges per kind of fix, with the thresholds they are measured against."""
    now = now or datetime.now(timezone.utc)
    return {
        "kinds": count(read_rows(conn), now),
        "clean_merges_needed": CLEAN_MERGES_NEEDED,
        "days_needed": DAYS_NEEDED,
        "revert_window_days": REVERT_WINDOW.days,
    }
