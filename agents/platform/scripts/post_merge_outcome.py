"""The post-merge check's last step and its schedule: reach one outcome per merged record, and run every step.

:func:`decide` weighs one merged record and returns the outcome it has reached,
with why, or None while it is in flight: ``verified_fixed`` (applied, healthy,
and the audit's check no longer fires), ``still_broken`` (the health window
failed, or the check still fires), ``applied_not_rechecked`` (applied, but the
workload or the re-check could not be read, or the install's scope leaves the
cluster out) and ``not_applied`` (not live by the kind's apply deadline).
:func:`decide_outcomes` stores each in ``outcome``, ``outcome_at`` and
``outcome_detail``, once; ``post_merge_report`` comments on it.

:func:`tick` is one run of the ``post-merge-check`` cron job: every step of
the check, in order. When the install's declared scope cannot be read, no
cluster is read that run.
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
from post_merge_report import (
    APPLIED_NOT_RECHECKED,
    APPLY,
    HEALTH,
    NOT_APPLIED,
    RECHECK,
    SCOPE,
    SCOPE_UNKNOWN,
    STILL_BROKEN,
    UNREADABLE,
    VERIFIED_FIXED,
)

logger = logging.getLogger("post_merge_check")

OUTCOMES = (VERIFIED_FIXED, STILL_BROKEN, APPLIED_NOT_RECHECKED, NOT_APPLIED)
#: How long after the merge the GitOps tool has to apply it, per kind of fix
#: (design gke-labs/kube-agents#5054, "Settings per fix").
APPLY_DEADLINE = {"no-pdb": timedelta(minutes=60)}
#: How long one cron run may keep starting work, so it ends inside its five-minute schedule
#: instead of holding the cron lock over the next ticks (a cluster hop can take 45 s).
RUN_BUDGET = timedelta(minutes=4)
#: How long after the apply the health window and the re-check may keep
#: failing to read the cluster before the record ends as not checked.
CHECK_DEADLINE = timedelta(minutes=60)

OPEN_COLUMNS = ("url", "repo", "number", "kind", "project", "location", "cluster", "namespace", "object",
                "merged_at", "merge_checked_at", "merge_commit", "applied_at", "sync_state", "health_result",
                "health_detail", "health_at", "health_checked_at")
#: What ``watch_health`` writes to ``health_detail`` when it fails a record it cannot judge
#: (``post_merge_check.WorkloadGone``), and the one such reason that is a judgement: the
#: workload's own reading was still not settled ``HEALTH_SETTLE_LIMIT`` past the window.
#: The same limit reached on a read that failed (``READ_FAILED``) judges nothing.
CANNOT_JUDGE = "cannot judge: "
NEVER_SETTLED = "not healthy "
READ_FAILED = "; this read failed:"
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


def _merge_seen(record: dict) -> datetime | None:
    """The later of the merge and when this job noticed it."""
    stamps = [t for t in (_parse_time(record.get("merged_at")), _parse_time(record.get("merge_checked_at"))) if t]
    return max(stamps) if stamps else None


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


def _read_since(record: dict, moment: datetime | None) -> bool:
    """Whether the apply step last read the record at or after ``moment``: a deadline ends a record
    only on a reading taken after it, never on a stale one (a pod outage, a cron gap, a backlog)."""
    read = _parse_time(record.get("health_checked_at"))
    return moment is not None and read is not None and read >= moment


def decide(record: dict, now: datetime, recheck: post_merge_scope.Recheck,
           scope_refusal: post_merge_scope.ScopeRefusal | None) -> tuple[str, str] | None:
    """The one outcome ``record`` has reached, with why (``<category>: <detail>``), or None while it is in flight."""
    if scope_refusal is not None and all(post_merge_check.cluster_coordinates(record)) and record["object"]:
        try:
            refusal = scope_refusal(record)
        except Exception as exc:  # noqa: BLE001 — judged as watch_health judges it: unknown, so it waits
            logger.warning("post-merge check: could not judge the scope of %s: %r", record["url"], exc)
            refusal = f"{post_merge_scope.UNKNOWN_PREFIX}the scope could not be judged: {exc!r}"
        if refusal and refusal.startswith(post_merge_scope.UNKNOWN_PREFIX):
            # Nothing is read; past every deadline the record ends as not checked.
            if _since_merge(record, now) > _apply_deadline(record) + CHECK_DEADLINE:
                return _not_live(record), f"{SCOPE_UNKNOWN}: {refusal[len(post_merge_scope.UNKNOWN_PREFIX):]}"
            return None
        if refusal:
            return _not_live(record), f"{SCOPE}: {refusal}"
    detail = str(record["health_detail"] or "")
    if detail.startswith(CANNOT_JUDGE + NEVER_SETTLED) and READ_FAILED not in detail:
        # Read every run and never healthy: the workload did not stay healthy after the apply.
        return STILL_BROKEN, f"{HEALTH}: {detail}"
    if detail.startswith(CANNOT_JUDGE):
        # Gone, or its baseline or apply time unreadable: nothing says whether the fix worked.
        return _not_live(record), f"{UNREADABLE}: {detail}"
    if record["applied_at"] is None:
        deadline = _apply_deadline(record)
        if not record["merge_commit"]:
            if _since_merge(record, now) > deadline:
                # The forge reported no merge commit, so the apply step never read this record.
                return NOT_APPLIED, f"{UNREADABLE}: the forge reported no merge commit, so the apply could not be read"
            return None
        merged = _merge_seen(record)
        if _since_merge(record, now) > deadline and _read_since(record, merged and merged + deadline):
            if str(record["sync_state"] or "").startswith(f"{UNREADABLE}:"):
                # The cluster did not answer: nothing is known about the GitOps tool.
                return NOT_APPLIED, f"{UNREADABLE}: the cluster could not be read before the deadline: {record['sync_state']}"
            return NOT_APPLIED, (f"{APPLY}: not live {int(deadline.total_seconds() // 60)} minutes after the merge: "
                                 f"{record['sync_state'] or 'no reading'}")
        return None
    window = HEALTH_WINDOW.get(record["kind"] or "", max(HEALTH_WINDOW.values()))
    stalled = (_since(record["applied_at"], now) or timedelta(0)) > window + CHECK_DEADLINE
    if record["health_result"] is None:
        applied = _parse_time(record["applied_at"])
        if stalled and _read_since(record, applied and applied + window + CHECK_DEADLINE):
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


def _locked(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()


def _stamp(conn: sqlite3.Connection, url: str, stamp: str) -> bool | str:
    """True once stamped; ``"locked"`` when another writer held the database; False otherwise."""
    try:
        with conn:
            conn.execute(f"UPDATE {RECORDS_TABLE} SET health_checked_at = ? WHERE url = ?", (stamp, url))
        return True
    except sqlite3.Error as exc:
        logger.warning("post-merge check: could not record the re-check's attempt for %s: %r", url, exc)
        return "locked" if _locked(exc) else False


def decide_outcomes(conn: sqlite3.Connection, recheck: post_merge_scope.Recheck, now: datetime | None = None,
                    scope_refusal: post_merge_scope.ScopeRefusal | None = None,
                    limit: int = post_merge_check.READS_PER_TICK,
                    stop: Callable[[], bool] | None = None) -> dict[str, list[str]]:
    """Reach an outcome for every merged record that has one; ``outcome``, ``outcome_at`` and ``outcome_detail``.

    Every open record is weighed each tick; deciding reads nothing but the
    re-check of a record whose window has passed, and at most ``limit`` of
    those run a tick. A re-check that did not answer stamps
    ``health_checked_at``, the order records are weighed in, so a record that
    keeps failing goes to the back and retries until :data:`CHECK_DEADLINE`
    rather than holding the tick's re-checks. ``stop()`` true starts no further
    record (``not_reached``). An outcome is written only over
    none, so it is never changed. A record this code fails on is returned
    under ``errors``.
    """
    now = now or datetime.now(timezone.utc)
    stamp = _now_text(now)
    seen: dict[str, list[str]] = {outcome: [] for outcome in OUTCOMES}
    seen["errors"] = []
    seen["deferred"] = []
    ran: list[str] = []

    def bounded(record: dict) -> dict:
        if len(ran) >= limit:
            raise post_merge_scope.RecheckDeferred
        ran.append(record["url"])
        return recheck(record)

    for values in conn.execute(OPEN_SQL).fetchall():
        if stop is not None and stop():
            # The run's time budget is spent: the rest are weighed next run.
            seen.setdefault("not_reached", []).append(values[0])
            continue
        record = dict(zip(OPEN_COLUMNS, values))
        try:
            reached = decide(record, now, bounded, scope_refusal)
        except Exception as exc:  # noqa: BLE001 — one record's fault must not stop the others
            logger.warning("post-merge check: could not decide %s: %r", record["url"], exc)
            seen["errors"].append(record["url"])
            continue
        if reached is None:
            if ran and ran[-1] == record["url"]:
                stamped = _stamp(conn, record["url"], stamp)
                if stamped is not True:
                    seen["deferred" if stamped == "locked" else "errors"].append(record["url"])
            continue
        outcome, detail = reached
        try:
            with conn:
                written = conn.execute(
                    f"UPDATE {RECORDS_TABLE} SET outcome = ?, outcome_at = ?, outcome_detail = ? "
                    "WHERE url = ? AND outcome IS NULL",
                    (outcome, stamp, detail[:500], record["url"]),
                ).rowcount
        except sqlite3.Error as exc:
            logger.warning("post-merge check: could not record the outcome of %s: %r", record["url"], exc)
            # A locked database is another writer, not a fault: the outcome is reached again next run.
            seen["deferred" if _locked(exc) else "errors"].append(record["url"])
            continue
        if written:
            seen[outcome].append(record["url"])
    return seen


#: A step run on the connection alone, so a caller can replace one.
Step = Callable[[sqlite3.Connection], dict]


def tick(conn: sqlite3.Connection, mode: str, *, view: post_merge_check.View = post_merge_check.forge_view,
         kubectl_json: post_merge_check.KubectlJson | None = None, recheck: post_merge_scope.Recheck | None = None,
         scope_refusal: post_merge_scope.ScopeRefusal | None = None, fill_edited: Step | None = None,
         fill_environments: Step | None = None, report: Step | None = None, followups: Step | None = None,
         reverts: Step | None = None, now: datetime | None = None, limit: int | None = None,
         budget: timedelta | None = None, clock: Callable[[], float] | None = None) -> dict[str, dict]:
    """One run of the ``post-merge-check`` cron job, every step in order.

    Notice merges (``post_merge_check.notice_merges``) and who edited them
    (``post_merge_edited``); wait for the apply and hold the health window
    (``post_merge_check.watch_health``); decide (:func:`decide_outcomes`); fill
    the environment of each outcome the clean-merge counter reads
    (``post_merge_environment``), before the follow-ups need it; report each
    outcome once in ``mode`` (``post_merge_report``); then step a still-broken
    fix down or offer auto-merge, and notice the reverts
    (``post_merge_followups``). The follow-ups run whatever
    ``KUBEAGENTS_AUTOMERGE`` says: the step-down reverts a labelled fix with
    the switch off, and the offer does nothing while it is off.

    When the install's declared scope cannot be read, no step that reads a
    cluster runs, and nothing is decided or followed up; merges are still
    noticed and outcomes already reached are still reported. A step that
    raises is kept under its name as ``{"errors": [...]}`` and the steps after
    it still run. Each step reads at most ``limit`` records,
    ``post_merge_check.reads_per_tick()`` when not given.

    The run stops starting work once ``budget`` (:data:`RUN_BUDGET`) has passed
    on ``clock`` (``time.monotonic``), so it ends inside the five-minute
    schedule rather than holding the cron lock over the next ticks: the apply
    and health step and the outcomes stop between records, and later steps are
    not started. What it left goes under ``deadline`` (``skipped`` steps,
    ``after_seconds``), and :func:`stopped_early` says so in the run's log. It
    is not an error: everything left waits for the next run.
    """
    import post_merge_edited
    import post_merge_environment
    import post_merge_followups
    import post_merge_report

    seen: dict[str, dict] = {}
    limit = post_merge_check.reads_per_tick() if limit is None else limit
    fill_edited = fill_edited or (lambda c: post_merge_edited.fill_human_edited(c, now=now, limit=limit))
    fill_environments = fill_environments or (lambda c: post_merge_environment.fill_environments(c, now=now, limit=limit))
    report = report or (lambda c: post_merge_report.report_outcomes(c, mode, now=now, limit=limit))
    followups = followups or (lambda c: post_merge_followups.run_followups(c, now=now, limit=limit))
    reverts = reverts or (lambda c: post_merge_followups.notice_reverts(c, now=now, limit=limit))

    import time

    clock = clock or time.monotonic
    started = clock()
    budget_s = (budget or RUN_BUDGET).total_seconds()

    def over() -> bool:
        return clock() - started >= budget_s

    def step(name: str, run: Callable[[], dict]) -> None:
        if over():
            left = seen.setdefault("deadline", {"after_seconds": [int(budget_s)], "skipped": []})
            left["skipped"].append(name)
            return
        try:
            seen[name] = run()
        except Exception as exc:  # noqa: BLE001 — one step's fault must not stop the others
            logger.warning("post-merge check: the %s step failed: %r", name, exc)
            seen[name] = {"errors": [f"{type(exc).__name__}: {exc}"[:300]]}

    step("notice", lambda: post_merge_check.notice_merges(conn, view, now, limit))
    step("edited", lambda: fill_edited(conn))
    kubeconfigs: dict[tuple[str, str, str], str] = {}
    kubectl_json = kubectl_json or post_merge_check.SandboxKubectl(kubeconfigs)
    scope_read = True
    if recheck is None or scope_refusal is None:
        try:
            scope = post_merge_scope.install_scope()
            scope_refusal = scope_refusal or post_merge_scope.scope_refusal_for(scope)
            recheck = recheck or post_merge_scope.sandbox_recheck(scope, kubeconfigs)
        except Exception as exc:  # noqa: BLE001 — an unknown scope reads no cluster
            logger.warning("post-merge check: the install's scope could not be read; no cluster is read: %r", exc)
            seen["scope"] = {"errors": [f"the install's scope could not be read: {exc}"[:300]]}
            scope_read = False
    if scope_read:
        refusal, rechecker = scope_refusal, recheck
        step("health", lambda: post_merge_check.watch_health(conn, kubectl_json, now, limit, scope_refusal=refusal,
                                                                  stop=over))
        step("outcomes", lambda: decide_outcomes(conn, rechecker, now, refusal, limit, stop=over))
        step("environments", lambda: fill_environments(conn))
    step("reports", lambda: report(conn))
    if scope_read:
        step("followups", lambda: followups(conn))
        step("reverts", lambda: reverts(conn))
    if any(seen.get(name, {}).get("not_reached") for name in ("health", "outcomes")):
        seen.setdefault("deadline", {"after_seconds": [int(budget_s)], "skipped": []})
    return seen


def stopped_early(seen: dict[str, dict]) -> str | None:
    """One log line when the run hit its time budget, naming what it left for the next run; else None."""
    left = seen.get("deadline")
    if not left:
        return None
    parts = [f"{len(seen[name]['not_reached'])} {name} record(s)" for name in ("health", "outcomes")
             if seen.get(name, {}).get("not_reached")]
    if left.get("skipped"):
        parts.append("the steps " + ", ".join(left["skipped"]))
    return (f"post-merge check: stopped early at its {left['after_seconds'][0]} s budget; left for the next run: "
            + ("; ".join(parts) or "nothing"))


def errored(seen: dict[str, dict]) -> list[str]:
    """Every record (or step fault) a tick's steps returned under ``errors``."""
    return [item for step in seen.values() if isinstance(step, dict) for item in step.get("errors") or []]


#: Where the cron run remembers which failures it has already reported, in the agent's home.
FAILURES_FILE = "post_merge_check_failures.json"
#: A failure already reported is reported again only after this long.
FAILURE_REPEAT = timedelta(days=1)


def failures_to_report(failures: list[str], now: datetime, path: str | None = None) -> list[str]:
    """The failures not reported within :data:`FAILURE_REPEAT`, remembered as reported now.

    The scheduler posts its failure line to chat on every non-zero exit, so a
    failure that persists (a scope that cannot be read, a record this code
    trips on) would otherwise post every five minutes. A run whose failures
    were all reported in the last day exits 0, and its summary still goes to
    the log. A state file that cannot be read or written reports everything,
    so a failure is never silenced by a broken file.
    """
    import json
    from pathlib import Path

    if path is None:
        import gitops_workspace

        path = str(Path(gitops_workspace.agent_home()) / FAILURES_FILE)
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
        reported = state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        reported = {}
    fresh = [f for f in failures
             if not ((when := _parse_time(reported.get(f))) and now - when < FAILURE_REPEAT)]
    if not fresh:
        return []
    kept = {f: t for f, t in reported.items() if (w := _parse_time(t)) and now - w < FAILURE_REPEAT}
    kept.update({f: _now_text(now) for f in fresh})
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(kept, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.warning("post-merge check: could not remember the reported failures in %s: %r", path, exc)
    return fresh
