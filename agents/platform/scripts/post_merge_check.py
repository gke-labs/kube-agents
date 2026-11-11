"""The post-merge check's steps over Session KV's ``post_merge_records`` (design gke-labs/kube-agents#5054).

Step 2, notice the merge: :func:`notice_merges` reads each waiting record's
pull request from the forge, one read per record per tick and at most
:data:`READS_PER_TICK` records, and writes what it found back to the row:

- merged: ``merged_at`` (the forge's close time, else now) and ``merge_commit``,
  the commit the merge became (the forge's ``mergeCommit``). The later steps
  ask the GitOps tool whether that commit is live.
- closed without a merge: ``closed_at``. It is read again for
  :data:`CLOSED_GRACE`, since closing and reopening is how some teams re-run
  CI: a reopen clears ``closed_at`` and a later merge is noticed. Past the
  grace the record ends, with no outcome and nothing said: a fix someone
  declined is not a failed fix.
- still open, or a read that failed: ``merge_checked_at`` only, which moves
  the record to the back of the queue so every waiting record is read in turn.

A record waits while it has no outcome, no ``merged_at`` and no ``closed_at``
older than the grace. It reads the forge per record rather than sharing the
pull-request sweep, whose list is not exposed to other callers; at the MVP's
scale (a handful of fixes, :data:`READS_PER_TICK` reads a tick) that costs
little. No model is involved, and nothing here posts: the reporter (a later
step) decides what to say, under ``KUBEAGENTS_POST_MERGE_CHECK``. Nothing
schedules it yet; ``post_merge_check.py notice`` is one tick.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("post_merge_check")

RECORDS_TABLE = "post_merge_records"
#: The most records one tick reads from the forge.
READS_PER_TICK = 20
#: The forge read's ceiling, per record, on the agent pod's hop to the sandbox
#: (``forge.call``); a run with no sandbox reaches the broker without one.
VIEW_TIMEOUT_SECONDS = 10.0
#: How long a pull request closed without a merge is still read, for a reopen.
CLOSED_GRACE = timedelta(days=1)

STATE_MERGED = "merged"
STATE_CLOSED = "closed"

CANDIDATES_SQL = (
    f"SELECT url, repo, number, closed_at, merge_checked_at, opened_at FROM {RECORDS_TABLE} "
    "WHERE outcome IS NULL AND merged_at IS NULL"
)

#: Reads one pull request: ``view(repo_url, number)`` -> the forge's proposal dict.
View = Callable[[str, int], dict]


def forge_view(repo: str, number: int) -> dict:
    """The forge's proposal for ``repo`` ``number``, through the broker (``forge.call``)."""
    import forge

    answer = forge.call(
        "proposal-view", {"number": number}, repo, retry_transient=True, timeout_s=VIEW_TIMEOUT_SECONDS
    )
    proposal = answer.get("proposal") if isinstance(answer, dict) else None
    if not isinstance(proposal, dict):
        raise TypeError("the forge answered without a proposal")
    return proposal


def _now_text(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def waiting(conn: sqlite3.Connection, now: datetime, limit: int = READS_PER_TICK) -> list[tuple[str, str, int]]:
    """``(url, repo, number)`` of the records to read this tick, least recently read first.

    A record closed within :data:`CLOSED_GRACE` still waits; one whose close
    time cannot be read is ended, as a close past the grace is.
    """
    picked = []
    for url, repo, number, closed_at, checked_at, opened_at in conn.execute(CANDIDATES_SQL):
        if closed_at is not None:
            closed = _parse_time(closed_at)
            if closed is None or now - closed > CLOSED_GRACE:
                continue
        if not repo or not isinstance(number, int):
            continue
        picked.append((checked_at is not None, checked_at or "", opened_at or "", url, repo, number))
    picked.sort()
    return [(url, repo, number) for _, _, _, url, repo, number in picked[:limit]]


def notice_merges(
    conn: sqlite3.Connection, view: View = forge_view, now: datetime | None = None, limit: int = READS_PER_TICK
) -> dict[str, list[str]]:
    """Read each waiting record's pull request once and record a merge or a close.

    Returns the urls under ``merged``, ``closed``, ``open`` and ``failed``.
    One record's failed read is logged and does not stop the others.
    """
    now = now or datetime.now(timezone.utc)
    stamp = _now_text(now)
    seen: dict[str, list[str]] = {"merged": [], "closed": [], "open": [], "failed": []}
    for url, repo, number in waiting(conn, now, limit):
        try:
            proposal = view(repo, number)
            state = proposal.get("state")
        except Exception as exc:  # noqa: BLE001 — one unreadable pull request must not stop the tick
            logger.warning("post-merge check: could not read %s: %r", url, exc)
            state, proposal = None, {}
        if state == STATE_MERGED:
            merge_commit = proposal.get("mergeCommit")
            update = (
                "merged_at = ?, merge_commit = ?, closed_at = NULL",
                (str(proposal.get("closed") or stamp), merge_commit if isinstance(merge_commit, str) and merge_commit else None),
                "merged",
            )
        elif state == STATE_CLOSED:
            update = ("closed_at = COALESCE(closed_at, ?)", (str(proposal.get("closed") or stamp),), "closed")
        elif state is not None:
            # Open, or reopened after a close: it waits again.
            update = ("closed_at = NULL", (), "open")
        else:
            update = (None, (), "failed")
        sets, values, bucket = update
        try:
            with conn:
                conn.execute(
                    f"UPDATE {RECORDS_TABLE} SET {sets + ', ' if sets else ''}merge_checked_at = ? WHERE url = ?",
                    (*values, stamp, url),
                )
        except sqlite3.Error as exc:
            logger.warning("post-merge check: could not record what %s is: %r", url, exc)
            bucket = "failed"
        seen[bucket].append(url)
    return seen


#: Session KV's database, as ``session_kv_server.py`` reads the same variable.
SESSION_KV_DB_ENV = "SESSION_KV_DB_PATH"
SESSION_KV_DB_DEFAULT = "/var/lib/kube-agents/session/session_kv.db"
SWITCH = "POST_MERGE_CHECK"


def main(argv: list[str] | None = None, view: View = forge_view) -> int:
    """``post_merge_check.py notice``: one tick of the merge step; prints what it saw as JSON.

    Reads nothing while the install's ``KUBEAGENTS_POST_MERGE_CHECK`` is ``off``.
    """
    import json
    import os
    import sys
    from contextlib import closing

    argv = sys.argv[1:] if argv is None else argv
    if argv != ["notice"]:
        print("usage: post_merge_check.py notice", file=sys.stderr)
        return 2
    import kubeagents_switches

    if kubeagents_switches.switch(SWITCH) == kubeagents_switches.OFF:
        print(json.dumps({"switch": kubeagents_switches.OFF}))
        return 0
    path = os.environ.get(SESSION_KV_DB_ENV) or SESSION_KV_DB_DEFAULT
    with closing(sqlite3.connect(path, timeout=5.0)) as conn:
        print(json.dumps(notice_merges(conn, view)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
