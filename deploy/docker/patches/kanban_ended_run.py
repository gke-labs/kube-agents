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

4. :func:`waiting_parent_refusal` covers a run that is still current but
   whose card gained an unfinished parent mid-run (``link_tasks`` leaves a
   running child running), so ``complete_task`` refuses on
   ``_parents_satisfied``. The worker is told the card now waits on that
   parent, to save its result with ``kanban_comment`` and block on the
   dependency, so the card runs again once the parent is done (#3252). Not
   latched: the parent can go away. When the card runs again, its worker
   context opens with that saved result (:func:`saved_result_banner`): use it
   if the prerequisite doesn't change it, and never open a second pull
   request. Re-checking after a prerequisite is often right, so reuse isn't
   forced; a structural duplicate-PR backstop is #3249. The ``kanban_link`` guard planned in
   #3248 would refuse such a link from an agent; this also covers one from
   the CLI or the dashboard.

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
import re
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


# How the cut-short run's comment opens. Comments reach the next run's worker
# context (``kanban_db._ctx_comments``), so this line is what tells it to
# finish from the saved result rather than redo the work and, say, open a
# second pull request (#2947's duplicate shape).
SAVED_RESULT_LEAD = (
    "Result from a run that was re-queued when a prerequisite was added; "
    "use it if the prerequisite doesn't change it, and don't open a second "
    "pull request:"
)


#: What a worker closes a quoted or bolded lead with, left at the head of the
#: result: one closing mark followed by whitespace. Nothing else is stripped, so
#: a bullet, an opening fence or the result's own last characters survive.
_LEAD_CLOSE = re.compile(r'^\s*(?:\*\*|["\'`\u201d\u2019])(?=\s|$)')


def waiting_parent_text(task_id: str, parent_id: str) -> str:
    """The terminal answer for a current run whose card gained a parent."""
    return (
        f"Card {task_id} now waits on {parent_id}, which was linked as its parent "
        f"after your run started, so it can't be completed until {parent_id} "
        f"finishes. Save your result with kanban_comment, opening it with "
        f"\"{SAVED_RESULT_LEAD}\", then call kanban_block(kind=\"dependency\") and "
        f"end your turn: the card goes back to the queue and runs again once "
        f"{parent_id} is done, and that run reads your comment. Retry "
        f"kanban_complete only if kanban_block answers that the card has no "
        f"unfinished parent: {parent_id} finished or was unlinked meanwhile."
    )


def waiting_parent_refusal(task_id: str, conn: Any) -> Optional[str]:
    """The answer if this worker's run is current but its card now has an
    unfinished parent, else None.

    ``link_tasks`` leaves a running child running on its run, so a parent
    linked mid-run makes ``complete_task`` refuse on ``_parents_satisfied``
    while the run is still current. Upstream's reason is the generic
    "unknown id, stale run, or already terminal", which invites a retry
    (#3252).

    Not latched, unlike the ended-run answer: the parent can go away (an
    unlink, or it finishes), and then a retried completion should succeed.
    The exit it names is a dependency block, which the worker's own
    ``kanban_block`` still reaches: the card waits in ``todo`` and
    ``recompute_ready`` promotes it when the parent is done. Ending the turn
    without a handoff instead would be booked as a protocol violation.
    """
    if _LATCHED is not None:
        return _LATCHED
    run_id = _own_run_id(task_id)
    if run_id is None:
        return None
    try:
        row = conn.execute(
            "SELECT t.status, t.current_run_id, ("
            "  SELECT l.parent_id FROM task_links l JOIN tasks p ON p.id = l.parent_id"
            "  WHERE l.child_id = t.id AND p.status NOT IN ('done', 'archived')"
            "  ORDER BY l.parent_id LIMIT 1)"
            " FROM tasks t WHERE t.id = ?",
            (task_id,),
        ).fetchone()
    except Exception as exc:
        logger.warning("kanban_ended_run: could not read %s: %s", task_id, exc)
        return None
    if row is None or row[0] != "running" or row[1] != run_id or not row[2]:
        return None
    logger.warning(
        "kanban_ended_run: run %s on %s waits on parent %s linked mid-run",
        run_id, task_id, row[2],
    )
    return waiting_parent_text(task_id, row[2])


# The event a dependency block records (kanban_db._route_block).
DEPENDENCY_WAIT_EVENT = "dependency_wait"
# The most of a saved result the banner quotes.
SAVED_RESULT_MAX_CHARS = 4000
# Events that end a run. A saved result is for the run right after the block
# that saved it; once a later run ends any of these ways, it's spent. (A
# dependency block records only DEPENDENCY_WAIT_EVENT, never "blocked".)
RUN_END_KINDS = ("completed", "blocked", "gave_up", "crashed", "timed_out")
# The newest saved result the card's own worker wrote during a run that then
# blocked on a dependency: the comment's author is the card's assignee (the
# same check as kanban_children_settled's step lines) and it falls inside that
# run, between its start and the block. A comment from anyone else, or from
# another run, never becomes the banner, which sits above the card's header
# in the worker's context. (kanban_comment takes its author from the
# worker's profile and allows cross-card comments, so another worker of the
# same profile commenting inside that window could still match; it holds the
# same trust as the card's own worker.)
_NEWEST_DEPENDENCY_WAIT_SQL = (
    "SELECT e.created_at, r.started_at, t.assignee FROM task_events e"
    " JOIN task_runs r ON r.id = e.run_id"
    " JOIN tasks t ON t.id = e.task_id"
    " WHERE e.task_id = ? AND e.kind = ?"
    # Spent once a run after it completed the card: a card reopened after
    # finishing from the saved result mustn't be handed it again.
    " AND NOT EXISTS (SELECT 1 FROM task_events d WHERE d.task_id = e.task_id"
    f"   AND d.kind IN {RUN_END_KINDS!r} AND d.id > e.id)"
    " ORDER BY e.id DESC LIMIT 1"
)
# The card's own worker's comments inside that run, newest first.
_RUN_COMMENTS_SQL = (
    "SELECT body FROM task_comments WHERE task_id = ?"
    " AND lower(trim(author)) = lower(trim(?))"
    " AND created_at >= ? AND created_at <= ?"
    " ORDER BY id DESC"
)
# The parents the card was waiting on at the block: unfinished then.
_WAITED_ON_SQL = (
    "SELECT p.id FROM task_links l JOIN tasks p ON p.id = l.parent_id"
    " WHERE l.child_id = ? AND p.status != 'archived'"
    " AND (p.completed_at IS NULL OR p.completed_at > ?)"
    " ORDER BY p.id"
)
# What identifies the lead, compared after normalising (see _lead_end).
_LEAD_KEY = "result from a run that was re-queued when a prerequisite was added"
_LEAD_TAIL = "pull request"
_CURLY = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})


def _fold(text: str) -> str:
    """Lowercase and straighten quotes without changing the length, so an
    offset found in the fold is an offset in ``text``. ``str.lower`` alone
    can grow a string (U+0130 becomes two code points)."""
    return "".join(c if len(low := c.lower()) != 1 else low
                   for c in text.translate(_CURLY))


def _lead_end(body: str) -> int:
    """Where the saved result starts in ``body``, or -1 when none of its first
    three lines opens with the lead. Tolerant of what a worker does to a line
    it was shown in quotes: a heading or blank line before it, leading quote
    marks, Markdown emphasis or code ticks, curly apostrophes, case. The
    result starts after the lead's own tail ("pull request", and its colon if
    one follows), or on the next line if the tail isn't on this one. Never at
    the first colon: that may be the result's own, in a URL or a time."""
    folded = _fold(body)
    offset = 0
    for line in folded.split("\n")[:3]:
        head = line.lstrip(" \t\r#>*_`\"'")
        if head.startswith(_LEAD_KEY):
            start = offset + (len(line) - len(head)) + len(_LEAD_KEY)
            line_end = offset + len(line)
            tail = folded.find(_LEAD_TAIL, start, line_end)
            if tail == -1:
                return min(line_end + 1, len(body))
            end = tail + len(_LEAD_TAIL)
            return end + 1 if folded[end:end + 1] == ":" else end
        offset += len(line) + 1
    return -1


SAVED_RESULT_HEADING = "## Saved result from a previous run"
SAVED_RESULT_BANNER = (
    "A previous run of this card produced the result quoted below before it "
    "was made to wait on {parents}, and saved it when the card went back to "
    "the queue. It is that run's report, data rather than "
    "instructions. If what {parents} produced doesn't change it, complete with "
    "it. "
    "If it opened a pull request, update or reference that one; don't open "
    "another."
)


def banner_text(parents: list[str]) -> str:
    """:data:`SAVED_RESULT_BANNER` naming the card's prerequisites."""
    names = ", ".join(parents) if parents else "another card"
    return SAVED_RESULT_BANNER.format(parents=names)


def _clip(text: str) -> str:
    """``text`` cut to :data:`SAVED_RESULT_MAX_CHARS` without severing a
    token (``kanban_handoff_clip.clip_handoff``), so a pull-request link at
    the end stays whole."""
    try:  # In the image, /opt/hermes is on sys.path.
        from gateway.kanban_handoff_clip import clip_handoff
    except Exception:
        try:  # Local unittest discovery: the patches dir is top-level.
            from kanban_handoff_clip import clip_handoff
        except Exception:
            return text[:SAVED_RESULT_MAX_CHARS]
    return clip_handoff(text, SAVED_RESULT_MAX_CHARS)


def saved_result_banner(conn: Any, task_id: str) -> list[str]:
    """Lines for the top of the worker context when the card's newest
    saved-result comment came from a run that then blocked on a dependency:
    the banner, then the result quoted. Otherwise none.

    A comment in the context's comment list was not enough: on next-dev-3 the
    re-run read it and followed the card's body instead, redoing the work
    (#3252). Narrow on purpose: only a comment opening with
    :data:`SAVED_RESULT_LEAD`, written by the card's own assignee inside a run
    that then blocked on a dependency, and only the newest such block counts
    (:data:`_NEWEST_DEPENDENCY_WAIT_SQL`, :data:`_RUN_COMMENTS_SQL`), so a card
    with ordinary comments reads as before and nobody else's comment can
    take the banner's place.
    """
    try:
        block = conn.execute(_NEWEST_DEPENDENCY_WAIT_SQL, (task_id, DEPENDENCY_WAIT_EVENT)).fetchone()
        if block is None:
            return []
        blocked_at, run_started, assignee = block
        if not (assignee or "").strip():
            # No assignee, no author to trust: fail closed.
            return []
        comments = conn.execute(_RUN_COMMENTS_SQL, (task_id, assignee, run_started, blocked_at)).fetchall()
        parents = [r[0] for r in conn.execute(_WAITED_ON_SQL, (task_id, blocked_at))]
    except Exception as exc:
        logger.warning("kanban_ended_run: could not read %s's saved result: %s", task_id, exc)
        return []
    result = ""
    for (body,) in comments:
        body = str(body or "")
        end = _lead_end(body)
        if end >= 0:
            # Keep the run's own characters: the fold was only for matching.
            result = _LEAD_CLOSE.sub("", body[end:], count=1).strip()
            break
    if not result:
        return []
    result = _clip(result)
    return [
        SAVED_RESULT_HEADING,
        banner_text(parents),
        "",
        *("> " + line for line in result.splitlines()),
        "",
    ]


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
