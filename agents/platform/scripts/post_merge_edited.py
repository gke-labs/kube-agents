"""Whether anyone but the install edited a merged fix, for the clean-merge counter (design gke-labs/kube-agents#5054).

``clean_merges.py`` counts a merge as clean only when its record's
``human_edited`` is ``0``: no commit but the agent's and no change request.
This fills that column: :func:`fill_human_edited` reads each merged record's
commits (``proposal-commits``) and reviews (``proposal-reviews``) through the
broker and sets ``1`` when anyone but the install committed to the pull request
or requested changes on it (a person or another automation, such as a
dependency bot), ``0`` when neither happened, by the pull-request sweep's own
rule (``vcs_readings.classify_touch``). A record whose commits or reviews cannot
be read in full stays ``NULL``, which the counter reads as not clean, and is
asked again next tick until :data:`EDITED_DEADLINE` after the check noticed the
merge (``merge_checked_at``, stamped by the read that found it); a merge
time that will not parse is not asked.

``post_merge_edited.py fill`` is one tick, at most
``post_merge_check.READS_PER_TICK`` pull requests, newest merge first. It reads
nothing while ``KUBEAGENTS_POST_MERGE_CHECK`` is ``off``. Nothing schedules it
yet. No model is involved, and nothing here posts.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import post_merge_check

logger = logging.getLogger(__name__)

#: How long after the check noticed the merge an unreadable record is still asked about.
EDITED_DEADLINE = timedelta(days=1)

Edited = Callable[[str, int], "bool | None"]

FILL_SQL = (
    f"SELECT url, repo, number, merged_at, merge_checked_at FROM {post_merge_check.RECORDS_TABLE} "
    "WHERE merged_at IS NOT NULL AND human_edited IS NULL ORDER BY merged_at DESC"
)


def forge_human_edited(repo: str, number: int) -> bool | None:
    """True when anyone but the install committed or requested changes; None when either cannot be read in full.

    A commit whose author is the install's login, or whose git email is one
    the install's commits carry, is the install's. Any other commit, a
    person's or another automation's, is an edit the install did not make; so
    is a review requesting changes from anyone but the install. A dismissed
    review is no longer its reviewer's say and is not counted.
    """
    import forge
    import repo_ref
    import vcs_readings

    def read(verb: str) -> dict:
        return forge.call(verb, {"number": number, "limit": 100}, repo, retry_transient=True,
                          timeout_s=post_merge_check.VIEW_TIMEOUT_SECONDS)

    try:
        login = (forge.call("identity", {}, repo, retry_transient=True,
                            timeout_s=post_merge_check.VIEW_TIMEOUT_SECONDS).get("identity") or {}).get("login") or ""
        commits = read("proposal-commits")
        reviews = read("proposal-reviews")
    except Exception as exc:  # noqa: BLE001 — unknown stays unknown, and the record is not clean
        logger.warning("post-merge check: could not read the commits and reviews of %s#%s: %r", repo, number, exc)
        return None
    if not login:
        # Without the install's login its own reviews and commits read as
        # someone else's; the pull-request sweep skips such a repository too.
        logger.warning("post-merge check: the broker named no install login for %s", repo)
        return None
    if commits.get("truncated") or reviews.get("truncated"):
        return None
    try:
        host = repo_ref.parse(repo).host or ""
    except repo_ref.RepoRefError:
        host = ""
    app_id = os.environ.get(vcs_readings.GITHUB_APP_ID_ENV, "").strip() or None
    emails = vcs_readings.install_commit_emails(host, login, app_id)
    events = [{**commit, "type": vcs_readings.EVENT_COMMIT} for commit in commits.get("commits") or []]
    events += [{**review, "type": vcs_readings.EVENT_REVIEW} for review in reviews.get("reviews") or []]
    edits = (vcs_readings.KIND_COMMIT, vcs_readings.KIND_REVIEW_CHANGES_REQUESTED)
    for event in events:
        touch = vcs_readings.classify_touch(event, login, emails)
        if touch is not None and touch.kind in edits:
            return True
    return False


def fill_human_edited(conn: sqlite3.Connection, edited: Edited = forge_human_edited, now: datetime | None = None,
                      limit: int = post_merge_check.READS_PER_TICK) -> dict[str, list[str]]:
    """``human_edited`` (1 or 0) for each merged record that has none yet.

    Returns the urls under ``edited``, ``clean``, ``unknown`` (left NULL, asked
    again next tick) and ``errors``. A record merged more than
    :data:`EDITED_DEADLINE` ago is no longer asked and costs none of ``limit``.
    The clock starts when the check noticed the merge, not at the forge's merge
    time, so a merge noticed late is still asked.
    """
    now = now or datetime.now(timezone.utc)
    seen: dict[str, list[str]] = {"edited": [], "clean": [], "unknown": [], "errors": []}
    asked = 0
    for url, repo, number, merged_at, noticed_at in conn.execute(FILL_SQL).fetchall():
        merged = post_merge_check._parse_time(noticed_at) or post_merge_check._parse_time(merged_at)
        if merged is None or now - merged > EDITED_DEADLINE:
            continue
        if asked >= limit:
            break
        asked += 1
        try:
            answer = edited(repo, int(number))
        except Exception as exc:  # noqa: BLE001 — one record's fault must not stop the others
            logger.warning("post-merge check: could not ask whether %s was edited: %r", url, exc)
            seen["errors"].append(url)
            continue
        if answer is None:
            seen["unknown"].append(url)
            continue
        try:
            with conn:
                conn.execute(f"UPDATE {post_merge_check.RECORDS_TABLE} SET human_edited = ? WHERE url = ?",
                             (int(answer), url))
        except sqlite3.Error as exc:
            logger.warning("post-merge check: could not record whether %s was edited: %r", url, exc)
            seen["errors"].append(url)
            continue
        seen["edited" if answer else "clean"].append(url)
    return seen


def main(argv: list[str] | None = None, edited: Edited = forge_human_edited) -> int:
    """``post_merge_edited.py fill``: one tick; prints what it saw as JSON.

    Reads nothing while the install's ``KUBEAGENTS_POST_MERGE_CHECK`` is ``off``.
    """
    import sys
    from contextlib import closing

    argv = sys.argv[1:] if argv is None else argv
    if argv != ["fill"]:
        print("usage: post_merge_edited.py fill", file=sys.stderr)
        return 2
    import kubeagents_switches

    if kubeagents_switches.switch(post_merge_check.SWITCH) == kubeagents_switches.OFF:
        print(json.dumps({"switch": kubeagents_switches.OFF}))
        return 0
    path = os.environ.get(post_merge_check.SESSION_KV_DB_ENV) or post_merge_check.SESSION_KV_DB_DEFAULT
    with closing(sqlite3.connect(path, timeout=5.0)) as conn:
        print(json.dumps(fill_human_edited(conn, edited)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
