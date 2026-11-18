"""The post-merge check's last step and its schedule: reach one outcome per merged record, and run every step.

:func:`decide` weighs one merged record and returns the outcome it has reached,
with why, or None while it is in flight: ``verified_fixed`` (applied, healthy,
and the audit's check no longer fires), ``still_broken`` (the health window
failed, or the check still fires), ``applied_not_rechecked`` (applied, but the
workload or the re-check could not be read, or the install's scope leaves the
cluster out) and ``not_applied`` (not live by the kind's apply deadline).
:func:`decide_outcomes` stores each in ``outcome``, ``outcome_at`` and
``outcome_detail``, once; nothing here comments on the pull request.

:func:`tick` is one run of the ``post-merge-check`` cron job: notice merges,
wait for the apply and hold the health window
(``post_merge_check.watch_health``), then decide. When the install's declared
scope cannot be read, no cluster is read that run.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import post_merge_check
import post_merge_scope
from post_merge_check import (
    HEALTH_FAILED,
    HEALTH_WINDOW,
    RECORDS_TABLE,
    _now_text,
    _parse_time,
)

logger = logging.getLogger("post_merge_check")

VERIFIED_FIXED = "verified_fixed"
STILL_BROKEN = "still_broken"
APPLIED_NOT_RECHECKED = "applied_not_rechecked"
NOT_APPLIED = "not_applied"
OUTCOMES = (VERIFIED_FIXED, STILL_BROKEN, APPLIED_NOT_RECHECKED, NOT_APPLIED)
#: How long after the merge the GitOps tool has to apply it, per kind of fix
#: (design gke-labs/kube-agents#5054, "Settings per fix").
APPLY_DEADLINE = {"no-pdb": timedelta(minutes=60)}
#: How long after the apply the health window and the re-check may keep
#: failing to read the cluster before the record ends as not checked.
CHECK_DEADLINE = timedelta(minutes=60)
#: The fixed reason categories ``outcome_detail`` starts with, so a reader
#: (the report comment, the clean-merge counter) chooses by category and never
#: parses the detail text.
SCOPE = "out of scope"
SCOPE_UNKNOWN = "scope unknown"
UNREADABLE = "unreadable"
HEALTH = "health"
RECHECK = "re-check"
APPLY = "apply"

OPEN_COLUMNS = ("url", "repo", "number", "kind", "project", "location", "cluster", "namespace", "object",
                "merged_at", "merge_checked_at", "merge_commit", "applied_at", "sync_state", "health_result",
                "health_detail", "health_at")
#: Merged records with no outcome. A pull request closed without a merge never
#: matches, so it never ends as ``not_applied`` (#5800): the merge step ends it
#: with no outcome once ``CLOSED_GRACE`` has passed.
OPEN_SQL = (
    f"SELECT {', '.join(OPEN_COLUMNS)} FROM {RECORDS_TABLE} WHERE outcome IS NULL AND merged_at IS NOT NULL "
    "ORDER BY health_checked_at IS NOT NULL, health_checked_at, merged_at"
)


def _since(stamp: object, now: datetime) -> timedelta | None:
    parsed = _parse_time(stamp)
    return None if parsed is None else now - parsed


def _since_merge(record: dict, now: datetime) -> timedelta:
    """How long since the merge, counted from when this job noticed it if that was later.

    ``merge_checked_at`` is last stamped by the read that saw the merge, so a
    merge noticed late (a forge outage, a backlog, a first run) still gets its
    whole apply deadline.
    """
    stamps = [t for t in (_parse_time(record.get("merged_at")), _parse_time(record.get("merge_checked_at"))) if t]
    return now - max(stamps) if stamps else timedelta(0)


def _apply_deadline(record: dict) -> timedelta:
    return APPLY_DEADLINE.get(record["kind"] or "", max(APPLY_DEADLINE.values()))


def _not_live(record: dict) -> str:
    return APPLIED_NOT_RECHECKED if record["applied_at"] else NOT_APPLIED


def decide(record: dict, now: datetime, recheck: post_merge_scope.Recheck,
           scope_refusal: post_merge_scope.ScopeRefusal | None) -> tuple[str, str] | None:
    """The one outcome ``record`` has reached, with why (``<category>: <detail>``), or None while it is in flight."""
    if scope_refusal is not None and all(post_merge_check.cluster_coordinates(record)):
        refusal = scope_refusal(record)
        if refusal and refusal.startswith(post_merge_scope.UNKNOWN_PREFIX):
            # Nothing is read; past every deadline the record ends as not checked.
            if _since_merge(record, now) > _apply_deadline(record) + CHECK_DEADLINE:
                return _not_live(record), f"{SCOPE_UNKNOWN}: {refusal[len(post_merge_scope.UNKNOWN_PREFIX):]}"
            return None
        if refusal:
            return _not_live(record), f"{SCOPE}: {refusal}"
    if str(record["health_detail"] or "").startswith("cannot judge"):
        return _not_live(record), f"{UNREADABLE}: {record['health_detail']}"
    if record["applied_at"] is None:
        deadline = _apply_deadline(record)
        if _since_merge(record, now) > deadline:
            if str(record["sync_state"] or "").startswith(f"{UNREADABLE}:"):
                # The cluster did not answer: nothing is known about the GitOps tool.
                return NOT_APPLIED, f"{UNREADABLE}: the cluster could not be read before the deadline: {record['sync_state']}"
            return NOT_APPLIED, (f"{APPLY}: not live {int(deadline.total_seconds() // 60)} minutes after the merge: "
                                 f"{record['sync_state'] or 'no reading'}")
        return None
    window = HEALTH_WINDOW.get(record["kind"] or "", max(HEALTH_WINDOW.values()))
    stalled = (_since(record["applied_at"], now) or timedelta(0)) > window + CHECK_DEADLINE
    if record["health_result"] is None:
        if stalled:
            return APPLIED_NOT_RECHECKED, (f"{UNREADABLE}: the workload could not be read after the apply: "
                                           f"{record['health_detail'] or 'no reading'}")
        return None
    if record["health_result"] == HEALTH_FAILED:
        return STILL_BROKEN, f"{HEALTH}: {record['health_detail']}"
    try:
        answer = recheck(record)
    except post_merge_scope.RecheckDeferred:
        return None
    except post_merge_scope.RecheckUnread as exc:
        if stalled:
            return APPLIED_NOT_RECHECKED, f"{UNREADABLE}: the re-check could not read the cluster: {exc}"
        logger.warning("post-merge check: the re-check of %s did not read; retrying: %s", record["url"], exc)
        return None
    except Exception as exc:  # noqa: BLE001 — a re-check that crashed is not a verdict
        # Retried each tick like an unread one: a crash can be a transient
        # fault (a timeout, a hop that dropped), and only the deadline ends it.
        if stalled:
            return APPLIED_NOT_RECHECKED, f"{RECHECK}: the re-check did not run: {exc}"
        logger.warning("post-merge check: the re-check of %s did not run; retrying: %r", record["url"], exc)
        return None
    outcome = answer.get("outcome")
    if outcome == "clean":
        return VERIFIED_FIXED, f"{RECHECK}: applied, healthy, and the audit's check no longer fires"
    if outcome == "reproduces":
        return STILL_BROKEN, f"{RECHECK}: the audit's check still fires on the workload"
    return APPLIED_NOT_RECHECKED, f"{RECHECK}: the re-check could not judge: {answer.get('reason') or 'no reason'}"


def _stamp(conn: sqlite3.Connection, url: str, stamp: str) -> bool:
    try:
        with conn:
            conn.execute(f"UPDATE {RECORDS_TABLE} SET health_checked_at = ? WHERE url = ?", (stamp, url))
        return True
    except sqlite3.Error as exc:
        logger.warning("post-merge check: could not record the re-check's attempt for %s: %r", url, exc)
        return False


def decide_outcomes(conn: sqlite3.Connection, recheck: post_merge_scope.Recheck, now: datetime | None = None,
                    scope_refusal: post_merge_scope.ScopeRefusal | None = None,
                    limit: int = post_merge_check.READS_PER_TICK) -> dict[str, list[str]]:
    """Reach an outcome for every merged record that has one; ``outcome``, ``outcome_at`` and ``outcome_detail``.

    Every open record is weighed each tick; deciding reads nothing but the
    re-check of a record whose window has passed, and at most ``limit`` of
    those run a tick. A re-check that did not answer stamps
    ``health_checked_at``, the order records are weighed in, so a record that
    keeps failing goes to the back and retries until :data:`CHECK_DEADLINE`
    rather than holding the tick's re-checks. An outcome is written only over
    none, so it is never changed. A record this code fails on is returned
    under ``errors``.
    """
    now = now or datetime.now(timezone.utc)
    stamp = _now_text(now)
    seen: dict[str, list[str]] = {outcome: [] for outcome in OUTCOMES}
    seen["errors"] = []
    ran: list[str] = []

    def bounded(record: dict) -> dict:
        if len(ran) >= limit:
            raise post_merge_scope.RecheckDeferred
        ran.append(record["url"])
        return recheck(record)

    for values in conn.execute(OPEN_SQL).fetchall():
        record = dict(zip(OPEN_COLUMNS, values))
        try:
            reached = decide(record, now, bounded, scope_refusal)
        except Exception as exc:  # noqa: BLE001 — one record's fault must not stop the others
            logger.warning("post-merge check: could not decide %s: %r", record["url"], exc)
            seen["errors"].append(record["url"])
            continue
        if reached is None:
            if ran and ran[-1] == record["url"] and not _stamp(conn, record["url"], stamp):
                seen["errors"].append(record["url"])
            continue
        outcome, detail = reached
        try:
            with conn:
                conn.execute(
                    f"UPDATE {RECORDS_TABLE} SET outcome = ?, outcome_at = ?, outcome_detail = ? "
                    "WHERE url = ? AND outcome IS NULL",
                    (outcome, stamp, detail[:500], record["url"]),
                )
        except sqlite3.Error as exc:
            logger.warning("post-merge check: could not record the outcome of %s: %r", record["url"], exc)
            seen["errors"].append(record["url"])
            continue
        seen[outcome].append(record["url"])
    return seen


def tick(conn: sqlite3.Connection, *, view: post_merge_check.View = post_merge_check.forge_view,
         kubectl_json: post_merge_check.KubectlJson | None = None, recheck: post_merge_scope.Recheck | None = None,
         scope_refusal: post_merge_scope.ScopeRefusal | None = None, now: datetime | None = None,
         limit: int | None = None) -> dict[str, dict]:
    """One run of the ``post-merge-check`` cron job: notice merges, wait for the apply, hold the window, decide.

    When the install's declared scope cannot be read, nothing is read from a
    cluster this run and nothing is decided; merges are still noticed. A step
    that raises is kept under its name as ``{"errors": [...]}`` and the steps
    after it still run. Each step reads at most ``limit`` records,
    ``post_merge_check.reads_per_tick()`` when not given.
    """
    seen: dict[str, dict] = {}
    limit = post_merge_check.reads_per_tick() if limit is None else limit

    def step(name: str, run: Callable[[], dict]) -> None:
        try:
            seen[name] = run()
        except Exception as exc:  # noqa: BLE001 — one step's fault must not stop the others
            logger.warning("post-merge check: the %s step failed: %r", name, exc)
            seen[name] = {"errors": [f"{type(exc).__name__}: {exc}"[:300]]}

    step("notice", lambda: post_merge_check.notice_merges(conn, view, now, limit))
    kubeconfigs: dict[tuple[str, str, str], str] = {}
    kubectl_json = kubectl_json or post_merge_check.SandboxKubectl(kubeconfigs)
    if recheck is None or scope_refusal is None:
        try:
            scope = post_merge_scope.install_scope()
            scope_refusal = scope_refusal or post_merge_scope.scope_refusal_for(scope)
            recheck = recheck or post_merge_scope.sandbox_recheck(scope, kubeconfigs)
        except Exception as exc:  # noqa: BLE001 — an unknown scope reads no cluster
            logger.warning("post-merge check: the install's scope could not be read; no cluster is read: %r", exc)
            seen["scope"] = {"errors": [f"the install's scope could not be read: {exc}"[:300]]}
            return seen
    refusal, rechecker = scope_refusal, recheck
    step("health", lambda: post_merge_check.watch_health(conn, kubectl_json, now, limit, scope_refusal=refusal))
    step("outcomes", lambda: decide_outcomes(conn, rechecker, now, refusal, limit))
    return seen


def errored(seen: dict[str, dict]) -> list[str]:
    """Every record (or step fault) a tick's steps returned under ``errors``."""
    return [item for step in seen.values() if isinstance(step, dict) for item in step.get("errors") or []]
