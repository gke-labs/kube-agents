"""Stop a card's worker when someone else blocks the card, and tell a worker
whose run has ended that it's over.

Installed into the image at ``/opt/hermes/tools/kanban_ended_run.py`` and
wired in by ``deploy/docker/patches/apply_kanban_ended_run.py`` at two files:
``hermes_cli/kanban_db.py`` (``block_task`` is wrapped by
:func:`stop_worker_on_external_block`) and ``tools/kanban_tools.py``
(``kanban_complete`` and ``kanban_block`` consult :func:`refuse_ended_run`
first, and :func:`ended_run_refusal` replaces the reason upstream reports
when the board refuses them).

Why
---
On the 2026-10-14 rehearsal (``ka-today-rehearsal``, main ``a81fd472``,
issue #2947) a platform card's first run exited without a handoff, the
dispatcher started a second run, and an operator blocked the card from the
CLI while that run was working. The second run's worker kept going for 41
minutes:

- ``kanban_block``, twice: "could not block ... (unknown id or not in
  running/ready)", because the card was already ``blocked``.
- ``kanban_complete``, five times: "worker exited cleanly (rc=0) without
  calling kanban_complete or kanban_block -- protocol violation. If the prior
  run already did the work, verify it and report the result via
  kanban_complete". That is the FIRST run's failure. When ``complete_task``
  returns False, upstream reports the card's ``last_failure_error``, whatever
  run it belongs to (``tools/kanban_tools.py``), so a worker whose run had been
  ended was told, in another run's words, to try again. It did.

Two upstream gaps made that possible:

1. ``block_task`` ends the card's run and clears ``worker_pid`` but leaves the
   OS process running. ``archive_task`` had the same gap and upstream closed it
   there (#76196: "Clearing ``worker_pid`` in the DB alone left the OS process
   running"), but not for a block.
2. A refused ``kanban_complete`` replays ``last_failure_error``. A worker's
   own run hasn't failed while it is still calling tools, so that text
   belongs to an earlier run, with one exception: a PR-acceptance refusal
   (``completion_contract``) writes the current run's reason there.

It is the same shape as #2947's first duplicate, a block that ended the run
but not the session, reached from the operator's side rather than the
worker's.

What this changes
-----------------
1. :func:`stop_worker_on_external_block` wraps ``block_task``. A block that
   names no ``expected_run_id`` comes from outside the card's run: the
   ``hermes kanban block`` CLI outside a worker, or the dashboard (upstream
   already refuses a worker blocking another card). Every worker-side
   caller (``kanban_block``, the CLI inside a worker, the goal loop, the
   rate-limit exit) passes its run id, or is the card's own worker by
   ``HERMES_KANBAN_TASK``. When an outside block lands on a
   ``running`` card, it is pinned to the run it saw, so the stop is contingent
   on this caller ending exactly that run, as ``archive_task`` does. The
   host-local worker is then stopped by upstream's
   ``_terminate_reclaimed_worker`` (SIGTERM, about 5 s for it to exit, then
   SIGKILL), a ``block_worker_termination`` event records the outcome, and a
   comment on the card says who blocked it and that the run was cut short.
2. :func:`refuse_ended_run` runs first in ``kanban_complete`` and
   ``kanban_block``. A dispatcher worker whose run id is no longer the card's
   current run, or whose card is no longer ``running``, gets one plain,
   terminal answer: the card isn't its to finish, stop now. The answer is
   latched in the process, so every later call returns it at once without
   touching the board, and the completion gates (which can write the submitted
   result onto the card) never run for a run that's over.
3. :func:`ended_run_refusal` replaces ``last_failure_error`` in the refusal
   text when the board refuses a worker's ``complete_task`` or ``block_task``,
   covering the run ending between the first check and the write. If the run
   is still current, ``kanban_complete`` reports :func:`current_run_failure`
   (this run's PR-acceptance refusal, the one reason upstream writes there for
   the run in progress) or upstream's generic reason.

Known windows, each caught by item 2 rather than a stop: a new run claiming
the card between the outside block's read and its write is blocked unpinned
and its worker isn't stopped, and a block that lands after a claim but before
the dispatcher records ``worker_pid`` has no process to stop.

Everything fails open: a board that can't be read, or a caller with no run id,
leaves upstream's behavior unchanged.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# The event a stop after an outside block records, alongside upstream's
# ``archive_worker_termination`` for the archive path.
TERMINATION_EVENT = "block_worker_termination"
COMMENT_AUTHOR = "kanban"

# Set by :func:`refuse_ended_run` the first time this process learns its run
# is over. A dispatcher worker runs one card's one run, so one value suffices.
_LATCHED: Optional[str] = None


def _own_run_id(task_id: str) -> Optional[int]:
    """This process's dispatcher run id, when it is the worker for ``task_id``."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def ended_run_text(task_id: str, status: Optional[str], current_run_id: Optional[int]) -> str:
    """The terminal answer for a worker whose run on ``task_id`` has ended."""
    if status == "running":
        why = f"another run (run {current_run_id}) has taken it over"
    elif status == "blocked":
        why = "it is blocked"
    elif status == "todo":
        why = "it was put back in the queue"
    elif status in ("done", "archived"):
        why = f"it is already {status}"
    else:
        why = f"it was moved out of your run (status: {status})"
    return (
        f"Card {task_id} is no longer yours to finish: {why}. Your run is "
        f"over. Stop now and end your turn. Don't call kanban_complete or "
        f"kanban_block again and don't redo the work; whoever moved the card "
        f"will pick it up from the board."
    )


def ended_run_refusal(task_id: str, conn: Any) -> Optional[str]:
    """The terminal answer if this worker's run on ``task_id`` has ended, else
    None. Latches the answer for the rest of the process."""
    global _LATCHED
    if _LATCHED is not None:
        return _LATCHED
    run_id = _own_run_id(task_id)
    if run_id is None:
        return None
    try:
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    except Exception as exc:
        logger.warning("kanban_ended_run: could not read %s: %s", task_id, exc)
        return None
    if row is None:
        return None
    status, current = row[0], row[1]
    if status == "running" and current == run_id:
        return None
    _LATCHED = ended_run_text(task_id, status, current)
    logger.warning(
        "kanban_ended_run: run %s on %s has ended (status %s, current run %s); "
        "refusing its handoffs from now on", run_id, task_id, status, current,
    )
    return _LATCHED


def current_run_failure(task_id: str, conn: Any) -> Optional[str]:
    """``last_failure_error`` when the card's current run wrote it: a failed
    ``pr_acceptance`` event on that run (``record_acceptance``). Else None."""
    try:
        row = conn.execute(
            "SELECT e.payload, t.last_failure_error FROM task_events e "
            "JOIN tasks t ON t.id = e.task_id "
            "WHERE e.task_id = ? AND e.kind = 'pr_acceptance' "
            "AND e.run_id = t.current_run_id ORDER BY e.id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    except Exception as exc:
        logger.warning("kanban_ended_run: could not read %s: %s", task_id, exc)
        return None
    if row is None or not row[1]:
        return None
    try:
        receipt = json.loads(row[0] or "{}")
    except ValueError:
        return None
    return row[1] if isinstance(receipt, dict) and receipt.get("ok") is False else None


def refuse_ended_run(task_id: str, connect: Callable[[], Any]) -> Optional[str]:
    """:func:`ended_run_refusal` on a board this opens and closes. For the top
    of ``kanban_complete`` and ``kanban_block``, before any gate runs."""
    if _LATCHED is not None:
        return _LATCHED
    if _own_run_id(task_id) is None:
        return None
    try:
        conn = connect()
    except Exception as exc:
        logger.warning("kanban_ended_run: could not open the board: %s", exc)
        return None
    try:
        return ended_run_refusal(task_id, conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _running_run(conn: Any, task_id: str) -> Optional[tuple]:
    """``(run_id, worker_pid, claim_lock)`` when ``task_id`` is running."""
    row = conn.execute(
        "SELECT status, current_run_id, worker_pid, claim_lock FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None or row[0] != "running" or row[1] is None:
        return None
    return row[1], row[2], row[3]


def _is_own_worker(task_id: str, pid: Optional[int]) -> bool:
    return os.environ.get("HERMES_KANBAN_TASK") == task_id or (pid is not None and pid == os.getpid())


def _blocker() -> str:
    """Who is blocking. Only the CLI outside a worker or the dashboard gets
    here; the CLI's own ``BLOCKED:`` comment carries the profile name."""
    return "the kanban CLI or dashboard"


def comment_text(run_id: int, termination: dict) -> str:
    who = _blocker()
    if termination.get("terminated"):
        how = "with SIGKILL" if termination.get("sigkill") else "with SIGTERM"
        return (
            f"Blocked by {who} while run {run_id} was still working. Its worker "
            f"(pid {termination.get('prev_pid')}) was stopped {how}, so whatever it "
            f"had in flight was cut short. Check the card's latest output before "
            f"unblocking; the next run starts fresh."
        )
    if termination.get("host_local") is False:
        return (
            f"Blocked by {who} while run {run_id} was still working. Its worker "
            f"runs on another host and wasn't stopped from here; its "
            f"kanban_complete and kanban_block are refused from now on."
        )
    return (
        f"Blocked by {who} while run {run_id} was still working. Stopping its "
        f"worker (pid {termination.get('prev_pid')}) failed; its kanban_complete "
        f"and kanban_block are refused from now on."
    )


def _stop_worker(kb: Any, conn: Any, task_id: str, run_id: int, pid: Optional[int],
                 claim_lock: Optional[str], terminate: Callable) -> None:
    """Stop the run's worker after the block committed, then record it.

    Post-commit, as in ``archive_task``: the wait for exit can take ~5 s and
    must not hold the write lock. Safe because the signal goes only to the pid
    the ended run recorded; a run the card gets next (after an unblock, or a
    dependency block's promotion from ``todo``) has its own process.
    """
    try:
        termination = terminate(pid, claim_lock)
    except Exception as exc:
        logger.warning("kanban_ended_run: stopping %s's worker failed: %s", task_id, exc)
        termination = {"prev_pid": pid, "error": str(exc)}
    termination = {**termination, "blocked_by": _blocker()}
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, TERMINATION_EVENT, termination, run_id=run_id)
        if pid:
            kb.add_comment(conn, task_id, COMMENT_AUTHOR, comment_text(run_id, termination))
    except Exception as exc:
        logger.warning("kanban_ended_run: recording %s's stop failed: %s", task_id, exc)


def _default_terminate(pid, claim_lock):
    from hermes_cli import kanban_db_dispatch as KD

    return KD._terminate_reclaimed_worker(pid, claim_lock)


def stop_worker_on_external_block(kb: Any, block_task: Callable, *,
                                  terminate: Callable = _default_terminate) -> Callable:
    """Wrap ``kb.block_task`` so a block from outside the card's run stops
    the run's live worker. ``kb`` is ``hermes_cli.kanban_db``."""

    @functools.wraps(block_task)
    def wrapped(conn, task_id, *, reason=None, kind=None, expected_run_id=None):
        if expected_run_id is not None:
            # The card's own run ending itself.
            return block_task(conn, task_id, reason=reason, kind=kind,
                              expected_run_id=expected_run_id)
        try:
            seen = _running_run(conn, task_id)
        except Exception as exc:
            logger.warning("kanban_ended_run: could not read %s: %s", task_id, exc)
            seen = None
        if seen is None or _is_own_worker(task_id, seen[1]):
            return block_task(conn, task_id, reason=reason, kind=kind)
        run_id, pid, claim_lock = seen
        # Pinned to the run seen, so the stop is for exactly the run this
        # call ended. If the card moved in between, block whatever is there
        # as upstream would, and stop nothing.
        if not block_task(conn, task_id, reason=reason, kind=kind, expected_run_id=run_id):
            return block_task(conn, task_id, reason=reason, kind=kind)
        _stop_worker(kb, conn, task_id, run_id, pid, claim_lock, terminate)
        return True

    wrapped.__kanban_ended_run__ = True
    return wrapped
