"""The track record: clean merges of each kind of fix, per repository and environment, from the post-merge records.

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

Merges are counted per ``(repository, kind, environment)``: a customer keeps a
workload repository, a cluster repository and many more per business group, and
trust earned in one of them says nothing about another. Repositories are compared
by :func:`repo_key`, so ``https://github.com/org/infra.git`` and
``https://github.com/org/infra`` are one. A record without a repository or a kind
is not counted; one without an environment counts under ``""``.

A ``(repository, kind, environment)`` is ``ready`` with :data:`CLEAN_MERGES_NEEDED`
clean merges on at least :data:`DAYS_NEEDED` different UTC days, so a single burst
does not earn it.

``session_kv_server.py`` serves :func:`track_record` as
``GET /v1/post-merge/clean-merges``. No HTTP here. It reads and never writes,
and nothing user-visible depends on it until a caller does.
"""

from __future__ import annotations

import re
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
REQUIRED_COLUMNS = ("repo", "kind", "environment", "outcome", "merged_at", "human_edited", "reverted_at")


class SchemaIncomplete(RuntimeError):
    """The records table exists but lacks a column the count needs."""


def repo_key(url: Any) -> str:
    """A repository url as one comparable key: host and path, lower-case, no scheme, user, ``.git`` or trailing slash."""
    text = str(url or "").strip().lower()
    text = re.sub(r"^[a-z+]+://", "", text)
    if "@" in text.split("/", 1)[0]:
        text = text.split("@", 1)[1].replace(":", "/", 1)
    return re.sub(r"(\.git)?/*$", "", text)


def _empty() -> dict[str, Any]:
    return {"clean": 0, "days": 0, "settled": 0, "ready": False}


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


def count(rows: list[dict[str, Any]], now: datetime) -> dict[tuple[str, str, str], dict[str, Any]]:
    """``{(repo_key, kind, environment): {repo, clean, days, settled, ready}}`` for every one with a record.

    ``repo`` is the first url seen for the key, for display.
    """
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    days: dict[tuple[str, str, str], set] = {}
    for row in rows:
        kind, key = row.get("kind"), repo_key(row.get("repo"))
        if not isinstance(kind, str) or not kind or not key:
            continue
        environment = row.get("environment") if isinstance(row.get("environment"), str) else ""
        group = (key, kind, environment)
        entry = groups.setdefault(group, {"repo": str(row.get("repo")).strip(), **_empty()})
        days.setdefault(group, set())
        if not is_clean(row, now):
            continue
        merged = _parse_time(row["merged_at"])
        entry["clean"] += 1
        days[group].add(merged.date())
        if now - merged > REVERT_WINDOW:
            entry["settled"] += 1
    for group, entry in groups.items():
        entry["days"] = len(days[group])
        entry["ready"] = entry["clean"] >= CLEAN_MERGES_NEEDED and entry["days"] >= DAYS_NEEDED
    return groups


def read_rows(conn: sqlite3.Connection, repo: str | None = None) -> list[dict[str, Any]]:
    """Every record's counted columns; an empty list when the table does not exist yet.

    With ``repo``, only the records of fixes in that repository (by :func:`repo_key`).
    Raises :class:`SchemaIncomplete` when the table lacks a :data:`REQUIRED_COLUMNS` column.
    """
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({RECORDS_TABLE})")}
    if not present:
        return []
    missing = [column for column in REQUIRED_COLUMNS if column not in present]
    if missing:
        raise SchemaIncomplete(f"{RECORDS_TABLE} has no column {', '.join(missing)}")
    cursor = conn.execute(f"SELECT {', '.join(REQUIRED_COLUMNS)} FROM {RECORDS_TABLE}")
    rows = [dict(zip(REQUIRED_COLUMNS, values)) for values in cursor.fetchall()]
    if repo is not None:
        wanted = repo_key(repo)
        rows = [row for row in rows if repo_key(row.get("repo")) == wanted]
    return rows


def ready_in(
    conn: sqlite3.Connection, repo: str, kind: str, environment: str, now: datetime | None = None
) -> dict[str, Any]:
    """``count``'s entry for one kind of fix in one repository and environment.

    The auto-merge offer is made per repository and environment, so five clean
    merges spread across two repositories, or across dev and prod, have earned
    it in neither. ``repo`` is the fix's repository; a key with no records is 0.
    """
    now = now or datetime.now(timezone.utc)
    entry = count(read_rows(conn, repo), now).get((repo_key(repo), kind, environment))
    return {key: value for key, value in entry.items() if key != "repo"} if entry else _empty()


def track_record(conn: sqlite3.Connection, now: datetime | None = None, repo: str | None = None) -> dict[str, Any]:
    """Clean merges per repository, kind of fix and environment, with the thresholds they are measured against.

    ``counts`` is a list sorted by ``(repo_key, kind, environment)``; ``repo``
    narrows it to one repository.
    """
    now = now or datetime.now(timezone.utc)
    counts = [
        {"repo_key": key, "kind": kind, "environment": environment, **entry}
        for (key, kind, environment), entry in sorted(count(read_rows(conn, repo), now).items())
    ]
    return {
        "counts": counts,
        "clean_merges_needed": CLEAN_MERGES_NEEDED,
        "days_needed": DAYS_NEEDED,
        "revert_window_days": REVERT_WINDOW.days,
    }
