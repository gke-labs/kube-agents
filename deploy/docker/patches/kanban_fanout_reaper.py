"""Settle a fan-out child that has run too long, so it stops holding a slot.

Installed into the image at ``/opt/hermes/tools/kanban_fanout_reaper.py`` and
wired into the dispatcher's reclaim phase (``_run_reclaim_phase`` in
``hermes_cli/kanban_db_dispatch.py``, right after upstream's
``enforce_max_runtime``) by ``deploy/docker/patches/apply_kanban_fanout_reaper.py``.

Why
---
A platform worker answering "where is X in the fleet" files one card per
cluster. On 2026-10-13 (#2978, ``pull-kube-agents-smoke-test-next`` build
``2110122651069452288``) it told each card to "wait for it to be found". The
workload existed on one cluster, so the cards on the others waited and never
finished. One of them, ``t_26cc6001``, held a worker slot for about 2.5 hours,
long after its case ended, while the dispatcher reported holding the person's
other cards back. The platform card couldn't complete either.

The persona text now says absent is a finding and never to tell a card to
wait. But that was the model's own wording, and prose has slipped before
(#2205), so this bounds it structurally. Upstream has nothing that does:
- ``enforce_max_runtime`` acts only on a card with ``max_runtime_seconds``, and
  stamping it would also raise that worker's ``TERMINAL_TIMEOUT`` to the limit
  less 30 s (``_worker_terminal_timeout_env``), turning the 180 s per-command
  bound into nearly the whole runtime;
- the stale-claim reclaim (``dispatch_stale_timeout_seconds``) acts only on a
  worker with no heartbeat, and a waiting worker keeps heartbeating;
- and upstream's timeout re-runs the card, which would start the same wait
  again.

What this does
--------------
On every dispatcher tick, a running per-cluster fan-out card whose active run
has lasted at least :data:`FANOUT_CHILD_MAX_SECONDS` is ended. A card counts as
one only when all of these hold, so a worker's other cards are left alone:
- a worker created it (a row in ``kanban_worker_children``, the attribution
  table ``kanban_children_settled`` writes);
- it's assigned to a Cluster Agent (``cluster-*``);
- its creator isn't one of its ``parents``. A follow-up queued with
  ``parents=[<own card>]`` is excluded. This is the same test
  ``kanban_children_settled`` and the scheduler use for a live child, so the
  three never disagree about which cards are fan-out work;
- a platform worker created it, as part of a burst: at least one other such
  card from the same creator within :data:`FANOUT_BURST_SECONDS` of it. The
  fan-out guidance has a platform worker file every per-cluster card up front,
  in one burst. So a single delegation (one deep RCA on one cluster), two
  delegations filed minutes apart, and a Cluster Agent's own child cards are
  excluded.

The burst is a proxy, and it can be wrong in one direction. A single delegation
filed within the window of a burst, or a retried creator that re-files inside
it, counts as fan-out. If such a card runs past the bound it's ended with the
"unchecked, not clear" text above, which is honest but stops a run that wasn't
waiting. Marking the fan-out explicitly when the cards are created is the real
fix and is tracked in #3373.

The card is ended like this: its worker is stopped the way
upstream stops one past its max runtime (SIGTERM, a short grace, SIGKILL), and
the card is completed with a result saying it didn't finish in time on that
cluster, keeping any draft the worker had already saved on the card, and a
``fanout_reaped`` event records it so the board can tell it from a real
completion. The parent's poll then sees it settled, and its answer can name the
gap. Heartbeats don't matter here.

Only fan-out children are touched. A card a person or a front door filed has no
attribution row and is never reaped, however long it runs. Only this process
life's own worker is stopped (the whole claim token, as the crash sweep in
``kanban_scheduling`` checks it). A card claimed on this host by a dispatcher
that has since restarted is settled without a kill, and another host's card is
left to its own dispatcher. Every read
and step fails open: a bookkeeping failure must never break the tick.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import time

try:
    from tools.kanban_children_settled import CHILDREN_TABLE
except ImportError:  # flat on the host, for the tests
    from kanban_children_settled import CHILDREN_TABLE
try:
    from hermes_cli.kanban_scheduling import claim_host, claim_is_self
except ImportError:
    from kanban_scheduling import claim_host, claim_is_self

logger = logging.getLogger(__name__)

#: How long a fan-out child may run before it's settled. The per-cluster cards
#: in the runs read for #2978 finished in 6-15 minutes; 30 leaves room for a
#: slow cluster and matches ``dispatch_stale_timeout_seconds``. One constant,
#: not a setting: it bounds a failure, it isn't a tuning knob.
FANOUT_CHILD_MAX_SECONDS = 30 * 60

#: How close together a platform worker's per-cluster cards must be created to
#: count as one fan-out. The guidance files them in one burst, seconds apart;
#: a second delegation decided on later is minutes away.
FANOUT_BURST_SECONDS = 120

#: Running per-cluster fan-out cards (see the module docstring for the four
#: conditions), with their active run's start and any stashed draft.
_OVERDUE_SQL = (
    "SELECT t.id, t.title, t.worker_pid, t.claim_lock, "
    "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
    "       COALESCE(t.result, '') AS draft "
    f"FROM tasks t JOIN {CHILDREN_TABLE} c ON c.child_id = t.id "
    "LEFT JOIN task_runs r ON r.id = t.current_run_id "
    "WHERE t.status = 'running' "
    "  AND t.assignee LIKE 'cluster-%' "
    "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
    "  AND NOT EXISTS (SELECT 1 FROM task_links l "
    "       WHERE l.parent_id = c.creator_id AND l.child_id = c.child_id) "
    "  AND EXISTS (SELECT 1 FROM tasks p WHERE p.id = c.creator_id AND p.assignee = 'platform') "
    f"  AND EXISTS (SELECT 1 FROM {CHILDREN_TABLE} s JOIN tasks st ON st.id = s.child_id "
    "       WHERE s.creator_id = c.creator_id AND s.child_id <> c.child_id "
    "         AND st.assignee LIKE 'cluster-%' "
    "         AND NOT EXISTS (SELECT 1 FROM task_links sl "
    "              WHERE sl.parent_id = s.creator_id AND sl.child_id = s.child_id) "
    f"         AND ABS(s.created_at - c.created_at) <= {FANOUT_BURST_SECONDS})"
)


def settle_text(title: str, limit_seconds: int = FANOUT_CHILD_MAX_SECONDS, draft: str = "") -> str:
    where = (title or "").strip() or "this cluster"
    text = (
        f"Did not finish within {limit_seconds // 60} minutes on {where}, so it "
        "was stopped to let the rest of the fan-out be answered. Treat this "
        "cluster as unchecked, not as clear."
    )
    draft = (draft or "").strip()
    if draft:
        text += "\n\nWhat it had drafted before it was stopped:\n\n" + draft
    return text


def _default_kill(pid: int, signal_fn=None) -> None:
    """Stop a worker the way upstream's ``enforce_max_runtime`` does."""
    from hermes_cli import kanban_db_dispatch as kd

    kill = kd._kill_fn(signal_fn)
    if kill is None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        kill(pid, signal.SIGTERM)
    kd._poll_worker_exit(pid)
    if kd._pid_alive(pid):
        kd._sigkill(kill, pid)


def _default_complete(conn, task_id: str, text: str, payload: dict) -> bool:
    from hermes_cli import kanban_db as kb

    if not kb.complete_task(conn, task_id, result=text, summary=text.split("\n", 1)[0]):
        return False
    # Its own event, so the board can tell a reaped card from a real finish.
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "fanout_reaped", payload)
    except Exception as exc:  # noqa: BLE001 — the completion already landed
        logger.warning("kanban_fanout_reaper: no fanout_reaped event for %s: %r", task_id, exc)
    return True


def _default_claimer_id() -> str:
    from hermes_cli import kanban_db as kb

    return kb._claimer_id()


def reap_overdue_fanout_children(
    conn,
    *,
    now: int | None = None,
    limit_seconds: int = FANOUT_CHILD_MAX_SECONDS,
    claimer_id: str | None = None,
    kill=None,
    complete=None,
) -> list[str]:
    """Settle overdue fan-out children; returns the ids it settled.

    ``kill``, ``complete`` and ``claimer_id`` default to upstream's own
    helpers and are parameters so a host test can stand them in.
    """
    settled: list[str] = []
    try:
        rows = conn.execute(_OVERDUE_SQL).fetchall()
    except Exception as exc:  # noqa: BLE001 — no table yet, a locked board
        logger.debug("kanban_fanout_reaper: board unreadable this tick: %r", exc)
        return settled
    if not rows:
        return settled
    now = int(time.time()) if now is None else int(now)
    try:
        me = _default_claimer_id() if claimer_id is None else claimer_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("kanban_fanout_reaper: no claimer id, skipping: %r", exc)
        return settled
    kill = kill or _default_kill
    complete = complete or _default_complete
    for row in rows:
        try:
            tid, title, pid, lock, started, draft = row[0], row[1], row[2], row[3], row[4], row[5]
            elapsed = now - int(started)
            if elapsed < limit_seconds:
                continue
            lock = str(lock or "")
            if claim_host(lock) != claim_host(me):
                continue  # another host's worker; its own dispatcher reaps it
            # Kill only this process life's own worker. A claim from this host
            # with another token is a dispatcher that died in place (the same
            # reading kanban_scheduling.classify_reclaim gives it). Its worker
            # went with the old PID namespace, and the recorded PID may now
            # name an unrelated process, so the card is settled without a kill.
            if pid and claim_is_self(lock, me):
                kill(int(pid))
            text = settle_text(title, limit_seconds, draft)
            payload = {"elapsed_seconds": elapsed, "limit_seconds": limit_seconds, "pid": pid}
            if complete(conn, tid, text, payload):
                settled.append(tid)
                logger.warning(
                    "kanban_fanout_reaper: settled %s (%s) after %ds; a fan-out "
                    "child may run %ds", tid, title, elapsed, limit_seconds,
                )
            else:
                logger.warning(
                    "kanban_fanout_reaper: %s (%s) was stopped after %ds but its "
                    "completion was refused; the dispatcher will reclaim it",
                    tid, title, elapsed,
                )
        except Exception as exc:  # noqa: BLE001 — one card must not stop the rest
            logger.warning("kanban_fanout_reaper: could not settle %r: %r", row[0], exc)
    return settled
