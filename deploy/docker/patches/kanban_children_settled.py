"""Refuse to close a card whose fanned-out children are still running.

Installed into the image at ``/opt/hermes/tools/kanban_children_settled.py``
and wired into ``tools/kanban_tools.py`` (the ``kanban_create`` and
``kanban_complete`` handlers) by
``deploy/docker/patches/apply_kanban_children_settled.py``.

Why
---
Completing a card IS the delivery: ``gateway/kanban_notifier.py`` posts a
terminal card's ``result`` into the subscribed chat thread as the answer to
whoever asked. On 2026-08-27 (issue #1010, eval ``capacity-pinned-pool-probe``,
build ``2093054394793725952``) the platform worker on ``t_470a97c5`` fanned
one investigation card out per cluster agent and then completed its own card
with::

    Fanned out cluster investigation tasks for `inference-server` to each
    cluster agent. Awaiting synthesis.

The gateway delivered that receipt, verbatim and marked "finished", as the
final answer. The question was never answered in the thread that asked it. A
sweep of 156 scored repetitions of the same case found the identical shape in
49 of them — different wording each time, so this is the protocol as the
worker understood it, not one bad sample.

The worker was doing what it was told: the fan-out guidance in
``agents/platform/SOUL.md`` said to file the per-cluster cards, file a fan-in
card, and "then complete your current card". Prompt guidance is also how the
two ancestors of this defect (#630, #802) were fixed, and both recurred. So
this patch enforces the delivery contract where it cannot be paraphrased away:
at the ``kanban_complete`` handler, the same seam ``kanban_result_required``
gates.

What this changes
-----------------
1. **The create handler records attribution.** A card created by a running
   worker (``HERMES_KANBAN_TASK`` set — the same definition of "worker" that
   ``kanban_auto_subscribe`` uses) is recorded in ``kanban_worker_children``
   as a child of the card that worker is running. Upstream stores no
   creator-of relationship: ``task_links`` is a *predecessor* edge, and the
   fan-out idiom deliberately creates children with no parents at all, so at
   completion time nothing else on the board connects the receipt to the work
   it dispatched.

2. **The complete handler checks it.** A completion of a card with recorded
   children that are neither settled (``done``/``archived``) nor gated on the
   completing card is refused once, with the instruction that fixes the run:
   wait for the children (poll ``kanban_show``, sleep between polls), then
   complete with the synthesized answer in ``result``. The error text is part
   of the fix — the worker reads it mid-run and self-corrects.

The exemption in 2 is load-bearing. Upstream's own continuation idiom —
"create a child of the current one (pass the current task id in ``parents``)
… then complete your own task" — files children that *cannot start* until the
completing card is done (``claim_task`` refuses them; see
``kanban_scheduling.py``). Refusing that completion would deadlock the board,
so a child with a ``task_links`` edge from the completing card is exempt.

Never wedging the card
----------------------
Same posture as ``kanban_result_required``, for the same reason: a gate that
can refuse forever is a worse bug than the one it fixes. The refusal is issued
at most once per :data:`NUDGE_TTL_SECONDS` per card; the retry is accepted
even if the children are still live. A worker at its iteration budget, or one
whose children are genuinely wedged, can still close its card — the guard
converts the silent contract violation into a corrected run when the model
cooperates, and into today's behaviour when it does not. It never converts it
into a stuck card. The deliberate cost: a worker that ignores the nudge and
resubmits its receipt unchanged ships the receipt, exactly as before the
patch.

A refusal never destroys the submitted text
-------------------------------------------
``kanban_result_required``'s module docstring records why its shape check was
deleted: a refusal that returns before ``kb.complete_task`` writes nothing,
so if the run ends between the refusal and the retry — a turn limit, a
cancellation — the only copy of the report dies with the worker's context.
This gate's window is wider than that one's was (the instruction is to poll
children for minutes, not to reformat), so it pays for its refusal up front:
the submitted ``result`` is stashed onto the card's ``result`` column, status
untouched, **before** the refusal is returned, and if that write fails the
completion is accepted instead — the gate refuses only when it has preserved
what it is refusing. A run that dies mid-wait therefore leaves the card open
with the best text available on it, which is strictly no worse than the
pre-patch behaviour of delivering that text as final; the accepted retry's
``kb.complete_task`` overwrites the stash with the real answer.

False positives, and why they are cheap
---------------------------------------
A worker can legitimately finish with a real answer while a card it spawned is
still running — follow-up work it queued for later rather than a piece of this
card's answer. Queued correctly (``parents=[<this card>]``) it is exempt.
Queued as a free-running card, the worker pays one nudge: it re-reads its
answer, resubmits, and the retry is accepted. One extra model turn — and only
the turn, because the refused submission is already stashed on the card —
bounded by the nudge memory. Set against a terminal "finished" message that
answers nothing — 31% of scored repetitions on the probe above — that trade
holds.

Fail-open throughout
--------------------
Every read here is advisory. A missing table (a board that predates the
patch), a locked database, a broken connection — anything short of a positive
"live children exist" lets the completion through, logged. A guard
bookkeeping failure must never block the one call that delivers a report.

Scope: the check lives in the ``kanban_complete`` tool handler, so the CLI and
the scheduler (``kb.complete_task`` direct writers) are unaffected — no cron
run and no human at a shell can be blocked by it. Attribution is written only
for creates made by a dispatcher-spawned worker, so an orchestrator filing
cards from a chat session (the Planning Agent's aggregation-card pattern in
``docs/designs/agent-communication.md``) is untouched.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

#: The env var the dispatcher pins into every worker it spawns: the id of the
#: card the worker is running. Presence (with a different id than the new
#: card) is the definition of "this create came from a worker" — the same
#: definition ``kanban_auto_subscribe`` keys subscription inheritance on.
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"

#: Fan-out attribution: which running card created which child. Upstream has
#: no such relationship (``task_links`` is a predecessor edge and fan-out
#: children are created parentless), so this patch owns the table. Created on
#: first write; boards that predate it read as "no children".
CHILDREN_TABLE = "kanban_worker_children"

#: Statuses that no longer owe anyone an answer. Matches ``SETTLED`` in
#: ``kanban_scheduling.py`` and the ``claim_task`` gate in
#: ``hermes_cli/kanban_db.py``.
SETTLED_STATUSES = ("done", "archived")

#: How long a refusal stays on file — one attempt window. The value and the
#: reasoning are ``kanban_result_required.NUDGE_TTL_SECONDS``: a model answers
#: a refused tool call in its next turn, seconds later, and 15 minutes is the
#: soonest an abandoned claim can pass to another worker, so a refusal older
#: than this belongs to a different attempt and that attempt gets its own
#: nudge.
NUDGE_TTL_SECONDS = 15 * 60

#: How many live children the refusal names. Enough to act on every real
#: fan-out (a fleet is a handful of clusters); a cap because the error travels
#: back through a tool result and a runaway creator should not get a runaway
#: refusal.
MAX_LISTED_CHILDREN = 10

#: Cards refused once and not yet completed, task id -> ``time.monotonic()``
#: at refusal. Keyed per card for the reason ``kanban_result_required``
#: documents: a shared gateway process must not spend card B's nudge on card
#: A. Entries are popped by the completion that answers them.
_refused_at: dict[str, float] = {}

_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {CHILDREN_TABLE} ("
    " child_id   TEXT PRIMARY KEY,"
    " creator_id TEXT NOT NULL,"
    " created_at INTEGER NOT NULL"
    ")",
    f"CREATE INDEX IF NOT EXISTS idx_{CHILDREN_TABLE}_creator "
    f"ON {CHILDREN_TABLE} (creator_id)",
)

#: Live children of the completing card, minus the continuation exemption:
#: a child the completing card gates (a ``task_links`` edge from it) cannot
#: start until this completion happens, so refusing on it would deadlock.
_LIVE_CHILDREN_SQL = (
    f"SELECT c.child_id, t.status FROM {CHILDREN_TABLE} c"
    " JOIN tasks t ON t.id = c.child_id"
    " WHERE c.creator_id = ?"
    f"  AND t.status NOT IN {SETTLED_STATUSES!r}"
    "  AND NOT EXISTS ("
    "        SELECT 1 FROM task_links l"
    "        WHERE l.parent_id = c.creator_id AND l.child_id = c.child_id"
    "  )"
    " ORDER BY c.child_id"
)

REFUSAL_HEADER = (
    "kanban_complete refused: {count} card(s) this card fanned out are not "
    "finished yet — {listing}. Completing this card now would deliver your "
    "`result` to the requester as the FINAL answer while the delegated work "
    "is still running; a dispatch receipt (\"fanned out, awaiting "
    "synthesis\") is not an answer, and nothing else delivers the children's "
    "findings to the person who asked. "
)

REFUSAL_INSTRUCTIONS = (
    "Do not complete yet. Instead: "
    "(1) wait for the children — poll each with kanban_show(<child id>), and "
    "run `sleep 60` between polling rounds rather than spinning; "
    "(2) when a child finishes, read its `result` and `metadata`; "
    "(3) once every child is settled, call kanban_complete again with the "
    "full synthesized answer to THIS card's request in `result`. "
    "If a child is blocked or keeps failing, escalate with "
    "kanban_block(kind=\"needs_input\") naming it, or complete with the "
    "answer you have and say plainly which part is missing and why. "
    "The `result` you just submitted has been saved on this card in the "
    "meantime, so it is not lost — but pass the full final text to "
    "kanban_complete again when you finish. "
    "(A follow-up card created with parents=[<this card id>] is exempt — it "
    "is queued to run after you finish and does not carry this card's "
    "answer.)"
)


def record_worker_child(conn, child_task_id: str, creator_task_id: str) -> bool:
    """Record that ``creator_task_id``'s worker created ``child_task_id``.

    Fail-soft and idempotent: ``INSERT OR IGNORE`` on the child primary key,
    so the card an idempotent ``kanban_create`` hands back (which may be days
    old and already attributed) keeps its first attribution. Returns whether
    a row was written. Never raises — attribution bookkeeping must never fail
    the ``kanban_create`` the worker is mid-flight on.
    """
    try:
        child = (child_task_id or "").strip()
        creator = (creator_task_id or "").strip()
        if not child or not creator or child == creator:
            return False
        for ddl in _TABLE_DDL:
            conn.execute(ddl)
        cur = conn.execute(
            f"INSERT OR IGNORE INTO {CHILDREN_TABLE}"
            " (child_id, creator_id, created_at) VALUES (?, ?, ?)",
            (child, creator, int(time.time())),
        )
        written = bool(cur.rowcount and cur.rowcount > 0)
        # kanban_db connections run autocommit with explicit BEGIN IMMEDIATE
        # for multi-statement writes; commit only when this write opened a
        # deferred transaction of its own (same dance as kanban_auto_subscribe).
        if getattr(conn, "in_transaction", False):
            conn.commit()
        return written
    except Exception as exc:  # noqa: BLE001 — never break the create
        logger.warning(
            "kanban_children_settled: recording %r as child of %r failed "
            "(continuing): %r",
            child_task_id, creator_task_id, exc,
        )
        return False


def maybe_record_worker_child(conn, child_task_id: str) -> bool:
    """Record attribution when this process is a dispatcher-spawned worker.

    The single call the patched ``kanban_create`` handler makes, immediately
    after ``kanban_auto_subscribe``'s subscription inheritance and under the
    same worker test. A process with no ``HERMES_KANBAN_TASK`` (chat session,
    CLI, cron) writes nothing.
    """
    creator = (os.environ.get(WORKER_TASK_ENV) or "").strip()
    if not creator:
        return False
    return record_worker_child(conn, child_task_id, creator)


def _format_refusal(children: list[tuple[str, str]]) -> str:
    listed = ", ".join(
        f"`{cid}` ({status})" for cid, status in children[:MAX_LISTED_CHILDREN]
    )
    overflow = len(children) - MAX_LISTED_CHILDREN
    if overflow > 0:
        listed += f" and {overflow} more"
    return (
        REFUSAL_HEADER.format(count=len(children), listing=listed)
        + REFUSAL_INSTRUCTIONS
    )


def _stash_submitted_result(conn, task_id: str, result) -> None:
    """Preserve a refused completion's ``result`` on the card, status untouched.

    Raises on failure — the caller answers a failed stash by accepting the
    completion, because a refusal that has not preserved what it refuses is
    the report-losing bug ``kanban_result_required``'s module docstring
    documents. A blank submission stashes nothing (there is nothing to lose).
    The accepted retry's ``kb.complete_task`` overwrites whatever is stashed.
    """
    text = "" if result is None else str(result)
    if not text.strip():
        return
    conn.execute(
        "UPDATE tasks SET result = ? WHERE id = ?", (text, task_id)
    )
    if getattr(conn, "in_transaction", False):
        conn.commit()


def require_children_settled(task_id, connect, result=None) -> str | None:
    """The completion gate. ``None`` when the completion may proceed.

    ``connect`` is ``hermes_cli.kanban_db.connect`` (injected by the applier's
    import trailer; injected so this module imports cleanly outside the
    image). The connection it returns is closed here. ``result`` is the
    completion's submitted result, passed so a refusal can preserve it on the
    card first — a refusal that cannot is not issued (see
    :func:`_stash_submitted_result`).

    Refused at most once per :data:`NUDGE_TTL_SECONDS` per card; the retry is
    accepted whatever the board says (see the module docstring on never
    wedging). One caveat on "at most once": the retry this gate waves through
    can still be refused by the result gate behind it, and the attempt after
    that arrives here with no memory and earns a second nudge. The alternation
    is bounded — each gate spends at most one nudge per gate per window, so a
    worker refused by both closes on its fourth call at worst — and it still
    cannot wedge. Fails open on any error: only a positive "live children
    exist" read, with the submitted result already stashed, refuses.
    """
    key = str(task_id or "").strip()
    if not key:
        return None
    # Popped rather than read: whether the refusal is being answered or has
    # gone stale, this call ends it (same pattern as kanban_result_required).
    refused_at = _refused_at.pop(key, None)
    nudge_spent = (
        refused_at is not None
        and time.monotonic() - refused_at <= NUDGE_TTL_SECONDS
    )

    try:
        conn = connect()
        try:
            rows = conn.execute(_LIVE_CHILDREN_SQL, (key,)).fetchall()
            children = [(str(r[0]), str(r[1])) for r in rows]
            if not children:
                return None
            if nudge_spent:
                logger.warning(
                    "kanban_children_settled: %s is completing over %d live "
                    "child(ren) on its retry; letting it close rather than "
                    "wedge the card",
                    key, len(children),
                )
                return None
            # The point of no return: nothing is refused until the submitted
            # text is safe on the board.
            _stash_submitted_result(conn, key, result)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — advisory gate, never block
        logger.warning(
            "kanban_children_settled: could not check %s's children or "
            "preserve its submitted result (allowing the completion): %r",
            key, exc,
        )
        return None

    _refused_at[key] = time.monotonic()
    return _format_refusal(children)
