"""The post-merge check's last step: report each record's outcome once (design gke-labs/kube-agents#5054).

``post_merge_check.py report`` is one tick of :func:`report_outcomes`. It takes
every ``post_merge_records`` row that has an ``outcome`` and no
``reported_at``, and acts on the install's ``KUBEAGENTS_POST_MERGE_CHECK``:

- ``silent`` posts nothing and stamps ``reported_at`` with
  :data:`SILENT_PREFIX`, so a record reached under ``silent`` is never
  commented later, when the switch moves to ``report``.
- ``report`` comments once on the fix pull request, from the fixed texts in
  :func:`comment_text`: verified fixed, still broken, applied but not
  re-checked, not checked, or not applied. A comment never carries the
  outcome's detail text, which can hold cluster output.

Every comment ends with :data:`COMMENT_MARKER`, a hidden line. Before posting,
the pull request's comments are read for it, so a comment that landed before
a crash, and before ``reported_at`` was written, is not posted twice. A pull
request with more comments than one read returns (``commentsTruncated``) is
not commented, since a second comment could not be ruled out. A comment the
forge refuses is retried behind the untried ones and given up after
:data:`REPORT_DEADLINE`. At most ``READS_PER_TICK`` pull requests are read a
tick. No model is involved.

The outcome names and the reason categories ``outcome_detail`` starts with
(``<category>: <detail>``) are defined here, since the comment is chosen by
them; the step that reaches an outcome writes them. Nothing schedules this step
yet, and no step writes ``outcome`` yet, so it reports nothing.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from post_merge_check import (
    READS_PER_TICK,
    RECORDS_TABLE,
    VIEW_TIMEOUT_SECONDS,
    _now_text,
    _parse_time,
)

logger = logging.getLogger("post_merge_check")

VERIFIED_FIXED = "verified_fixed"
STILL_BROKEN = "still_broken"
APPLIED_NOT_RECHECKED = "applied_not_rechecked"
NOT_APPLIED = "not_applied"
#: The fixed reason categories ``outcome_detail`` starts with.
SCOPE = "out of scope"
SCOPE_UNKNOWN = "scope unknown"
UNREADABLE = "unreadable"
HEALTH = "health"
RECHECK = "re-check"
APPLY = "apply"
#: The switch's value that comments; ``silent`` and anything else post nothing.
MODE_REPORT = "report"
#: ``reported_at`` prefixes for an outcome not commented: reached under
#: ``silent``, a pull request whose comments are too many to read in one page,
#: and a comment the forge kept refusing past :data:`REPORT_DEADLINE`.
SILENT_PREFIX = "silent:"
UNREAD_PREFIX = "unread:"
GAVE_UP_PREFIX = "failed:"
#: ``reported_at`` for a comment the forge refused, while it is still retried:
#: the stamp sends the record behind the ones not yet tried.
RETRY_PREFIX = "retry:"
REPORT_DEADLINE = timedelta(days=1)
#: The hidden line every comment carries.
COMMENT_MARKER = "<!-- kube-agents:post-merge-check -->"
#: How many comments one ``proposal-view`` read returns; more is ``commentsTruncated``.
COMMENTS_LIMIT = 100
SHORT_SHA = 12

REPORT_COLUMNS = ("url", "repo", "number", "cluster", "namespace", "object", "merge_commit", "outcome",
                  "outcome_detail", "outcome_at")
REPORT_SQL = (
    f"SELECT {', '.join(REPORT_COLUMNS)} FROM {RECORDS_TABLE} WHERE outcome IS NOT NULL "
    f"AND (reported_at IS NULL OR reported_at LIKE '{RETRY_PREFIX}%') "
    "ORDER BY reported_at IS NOT NULL, reported_at, outcome_at"
)

#: Posts one comment: ``comment(repo, number, body)``.
Comment = Callable[[str, int, str], None]
#: Whether a pull request already carries the marker: ``has_marker(repo, number)`` -> True, False, or
#: None when its comments are too many to read in one page.
HasMarker = Callable[[str, int], "bool | None"]


def _short(sha: object) -> str:
    return str(sha or "")[:SHORT_SHA]


def comment_text(record: dict) -> str:
    """The one comment for an outcome: fixed text by outcome and category, the object's name and the
    merge commit. Never the detail text, which can carry cluster output."""
    workload = f"`{record['object']}` in `{record['namespace'] or 'default'}` on `{record['cluster']}`"
    commit = f"`{_short(record['merge_commit'])}`" if record["merge_commit"] else "the merge"
    category = str(record["outcome_detail"] or "").split(":", 1)[0]
    outcome = record["outcome"]
    if category == SCOPE:
        line = (f"**Not checked.** `{record['cluster']}` left this install's declared scope, so kube-agents did "
                "not read it and this fix is not verified.")
    elif category == SCOPE_UNKNOWN:
        line = (f"**Not checked.** kube-agents could not confirm that `{record['cluster']}` is in this install's "
                "declared scope, so it did not read it and this fix is not verified.")
    elif category == UNREADABLE and outcome == NOT_APPLIED:
        line = f"**Not checked.** kube-agents could not read {workload} after the merge, so this fix is not verified."
    elif category == UNREADABLE:
        line = (f"**Applied, not checked.** {commit} is live, but kube-agents could not read {workload} "
                "afterwards, so this fix is not verified.")
    elif outcome == VERIFIED_FIXED:
        line = (f"**Verified fixed.** kube-agents checked this fix after it merged: {commit} is live, {workload} "
                "stayed healthy through the check, and the audit's check no longer fires on it.")
    elif outcome == STILL_BROKEN and category == RECHECK:
        line = f"**Still broken.** {commit} is live, but the audit's check still fires on {workload}."
    elif outcome == STILL_BROKEN:
        line = f"**Still broken.** {commit} is live, but {workload} did not stay healthy through the window after the apply."
    elif outcome == APPLIED_NOT_RECHECKED:
        line = (f"**Applied, not re-checked.** {commit} is live and {workload} stayed healthy, but the audit's "
                "check could not be re-run, so this fix is not verified.")
    else:
        line = (f"**Not applied.** The GitOps tool did not report {commit} live for {workload} within the deadline, "
                "so the fix was not checked.")
    return f"{line}\n\n{COMMENT_MARKER}"


def forge_comment(repo: str, number: int, body: str) -> None:
    """Post ``body`` on ``repo`` ``number`` through the broker (``forge.call``)."""
    import forge

    forge.call("proposal-comment", {"number": number, "body": body}, repo, timeout_s=VIEW_TIMEOUT_SECONDS * 3)


def forge_has_marker(repo: str, number: int, marker: str = COMMENT_MARKER) -> bool | None:
    """Whether a comment this install wrote on ``repo`` ``number`` carries ``marker``; None when the read
    returned only some of the comments and none of those does.

    Only the install's own comments count (the ``identity`` login, as
    ``post_merge_stepdown`` checks), so someone else pasting the marker cannot
    silence the report. Raises when the forge does not say who the install is.
    """
    import forge

    viewer = forge.normalise_login(
        str((forge.call("identity", {}, repo, retry_transient=True, timeout_s=VIEW_TIMEOUT_SECONDS) or {})
            .get("identity", {}).get("login") or "")
    )
    if not viewer:
        raise RuntimeError("the forge did not say who this install is")
    answer = forge.call("proposal-view", {"number": number, "comments": True, "limit": COMMENTS_LIMIT}, repo,
                        retry_transient=True, timeout_s=VIEW_TIMEOUT_SECONDS * 3)
    for comment in answer.get("comments") or []:
        author = comment.get("author")
        login = author.get("login") if isinstance(author, dict) else author
        if marker in str(comment.get("body") or "") and forge.normalise_login(str(login or "")) == viewer:
            return True
    return None if answer.get("commentsTruncated") else False


def _since(stamp: object, now: datetime) -> timedelta | None:
    parsed = _parse_time(stamp)
    return None if parsed is None else now - parsed


def _stamp(conn: sqlite3.Connection, url: str, reported: str) -> bool:
    """Write ``reported_at``; False (logged) when the database refused."""
    try:
        with conn:
            conn.execute(f"UPDATE {RECORDS_TABLE} SET reported_at = ? WHERE url = ?", (reported, url))
    except sqlite3.Error as exc:
        logger.warning("post-merge check: could not record the report for %s: %r", url, exc)
        return False
    return True


def report_outcomes(conn: sqlite3.Connection, mode: str, comment: Comment = forge_comment,
                    has_marker: HasMarker = forge_has_marker, now: datetime | None = None,
                    limit: int = READS_PER_TICK) -> dict[str, list[str]]:
    """Report each new outcome once: ``report`` comments on the pull request, any other mode posts nothing.

    Returns the urls by what happened: ``commented``, ``silent``, ``unread``
    (too many comments to check), ``failed`` (refused, retried next tick),
    ``given_up`` (refused past :data:`REPORT_DEADLINE`) and ``errors`` (given
    up, or ``reported_at`` could not be written).
    """
    now = now or datetime.now(timezone.utc)
    stamp = _now_text(now)
    seen: dict[str, list[str]] = {
        "commented": [], "silent": [], "failed": [], "unread": [], "given_up": [], "errors": []
    }
    tried = 0
    for values in conn.execute(REPORT_SQL).fetchall():
        record = dict(zip(REPORT_COLUMNS, values))
        url = record["url"]
        bucket, reported = "silent", SILENT_PREFIX + stamp
        if mode == MODE_REPORT:
            if tried >= limit:
                break
            tried += 1
            try:
                present = has_marker(record["repo"], int(record["number"]))
                if present is None:
                    bucket, reported = "unread", UNREAD_PREFIX + stamp
                else:
                    if not present:
                        comment(record["repo"], int(record["number"]), comment_text(record))
                    bucket, reported = "commented", stamp
            except Exception as exc:  # noqa: BLE001 — retried next tick; the marker stops a double post
                logger.warning("post-merge check: could not comment on %s: %r", url, exc)
                if (_since(record["outcome_at"], now) or timedelta(0)) <= REPORT_DEADLINE:
                    bucket, reported = "failed", RETRY_PREFIX + stamp
                else:
                    bucket, reported = "given_up", GAVE_UP_PREFIX + stamp
                    seen["errors"].append(url)
        if not _stamp(conn, url, reported):
            seen["errors"].append(url)
            continue
        seen[bucket].append(url)
    return seen
