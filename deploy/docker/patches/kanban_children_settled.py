"""Refuse to close a card whose fanned-out children are still running, or
whose declared steps are not all done, or to re-queue one with nothing to wait
on.

Installed into the image at ``/opt/hermes/tools/kanban_children_settled.py``
and wired into ``tools/kanban_tools.py`` (the ``kanban_create``,
``kanban_complete`` and ``kanban_block`` handlers) by
``deploy/docker/patches/apply_kanban_children_settled.py``.

Why
---
Completing a card IS the delivery: ``gateway/kanban_notifier.py`` (this
repository's companion module, wired into upstream's
``gateway/kanban_watchers_notifier.py``) posts a terminal card's ``result``
into the subscribed chat thread as the answer to whoever asked. On 2026-08-27 (issue #1010, eval ``capacity-pinned-pool-probe``,
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

A dependency block on a card with nothing to wait on
----------------------------------------------------
The other way a worker "waits" for its fan-out is
``kanban_block(kind="dependency")``, and upstream routes that block to
``todo`` rather than ``blocked`` (``_route_block`` in
``hermes_cli/kanban_db.py``) for ``recompute_ready`` to promote once the
card's parents are done. Fan-out children are not parents, so a card whose
worker blocks this way has nothing to wait for: the next dispatcher tick
promotes it and a fresh worker claims it. The block ended the first run but
not the first session, which keeps its tools; the fresh one starts from the
card body with no record of what the first did. On 2026-10-08 (#2704, build
``2108234393209278464``, card ``t_b999714f``) that turned one requested fix
into two pull requests: the first worker opened #250, was refused here, blocked
with ``dependency`` against this gate's own instruction to use
``needs_input``, and six seconds later a new worker claimed the card and opened
#251. ``agents/platform/SOUL.md`` forbids the same move in prose.

So the ``kanban_block`` handler refuses ``kind="dependency"`` when the card
has nothing unfinished to wait on: no parent that isn't settled, and no child
that ``kanban_scheduling``'s repair will turn into a parent inside
``block_task``. The second test is the repair's own predicate,
``find_deadlocked_children``, not "any linked child": an edge it won't flip (a
successor someone planned before this card started, or a child that still
waits on another card) leaves the card with nothing to wait on, and it would
re-queue as fast as an unlinked one.
The refusal is not rationed like the completion gate's, because it can't
wedge anything: the worker still holds its card and can poll, complete, or
block with any other kind. Every read fails open.

A card whose declared steps are not all done
--------------------------------------------
On 0.8.0 (#2935, card ``t_bb95580d``, "Provision new GKE cluster with Agent
Substrate installed") the worker created the cluster, failed to install
Substrate, and called ``kanban_complete``. The thread showed ``done``, the
cluster kept billing, and the software the card named was never installed.
``agents/platform/SOUL.md`` already said a new cluster goes through a pull
request; prose did not stop it, which is the pattern #630, #802 and #1010 set
before it.

So a card can carry a plan, one ``step <key>: <status>`` line per deliverable,
and ``kanban_complete`` is refused while any step's latest status is not
``done``. The creator writes the plan into the card's ``body`` when it files
the card, and the worker records progress with ``kanban_comment``; the gate
reads both, body first and then comments in order, and the latest line per
key wins. Either side declaring a step is enough to hold the card, so an
ungated completion needs the creator and the worker to both skip the rule.
Comments, not ``metadata``: upstream writes a card's metadata only in the
``kanban_complete`` call itself, so a plan kept there would be written by the
call it is meant to check. Comments also survive a respawn, since a fresh
worker is handed the card's recent comments.

The statuses are ``planned``, ``done`` and ``failed``, and nothing else
settles a step. There is no ``skipped``: a step a worker could skip and then
complete over is #2935 again with one more line. A step that turned out to
be unneeded, or failed, ends the card in ``kanban_block``, whose ``reason``
names what was not delivered and what was left behind (#2939).

This refusal is not rationed like the fan-out one. A live child can outlast
its creator's worker, so that gate gives way on the retry; a step is the
worker's own to finish, and ``kanban_block`` is always open to it, so a
standing refusal cannot wedge the card. A worker that neither finishes nor
blocks runs out of turns. The worker-exit path records that run as a counted
failure, upstream hands the card to one fresh worker (which inherits the
step comments, so it sees what was planned and what failed), and a second
such run ends the card ``blocked`` by upstream's failure limit. None of those
paths is ``done``. A card with no step line anywhere completes as before.
Every read fails open.

Only the assignee's comments count. A comment's ``author`` is the writer's
profile name, and the front door (the ``default`` profile) comments on a
``needs_input`` card with the user's answer verbatim, so a thread reply such as
``step substrate: done`` would otherwise rewrite the worker's record. The worker
and the card that filed it are usually the same profile, so the gate cannot
tell those two apart. The gate makes an undelivered completion an explicit
false statement rather than an easy one; it does not stop a worker that writes
``done`` over a step it did not do.

Scope: the checks live in the ``kanban_complete`` and ``kanban_block`` tool
handlers, so the CLI and the scheduler (``kb.complete_task`` and
``kb.block_task`` direct writers) are unaffected — no cron
run and no human at a shell can be blocked by it. Attribution is written only
for creates made by a dispatcher-spawned worker, so an orchestrator filing
cards from a chat session (the Planning Agent's aggregation-card pattern in
``docs/designs/agent-communication.md``) is untouched.
"""

from __future__ import annotations

import logging
import os
import re
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

#: Statuses that no longer owe anyone an answer. Matches
#: ``kanban_scheduling.CHILD_SETTLED_STATUSES`` — reconciled against this tuple
#: at build time by ``apply_kanban_scheduling`` — and the ``claim_task`` gate in
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

#: A step line: ``step <key>: <status>``, anywhere in a card's body or a
#: comment, one per line, optionally as a list item. The key starts with a
#: letter and is a short slug, and the status word must end the line or be
#: followed by a separated reason (`` - ...``, ``: ...``, `` (...)``, ``, ...``,
#: ``; ...``) or up to three closing marks (``.``, ``!``, an emoji), so prose
#: such as "Step 1: Planned maintenance is Sunday" or "Step two: failed pods are
#: restarted" is not a plan, and "done-ish" is not ``done``. A list item may be
#: a checklist box (``- [ ] step ...``). Backticks and ``*`` are stripped before
#: matching (:data:`STEP_MARKUP_RE`), so ``step `substrate`: **done**`` reads as
#: ``step substrate: done``.
STEP_LINE_RE = re.compile(
    r"^[ \t>*-]*(?:\d+[.)][ \t]*)?(?:\[[ xX]\][ \t]*)?step[ \t]+([a-z][a-z0-9-]{0,39})[ \t]*:[ \t]*"
    r"(planned|done|failed)"
    r"(?=[ \t]*[^\w\s-]{0,3}[ \t]*\r?$|[ \t]*[,;][ \t]|[ \t]+[-\u2013\u2014(]|[ \t]*[\u2013\u2014:(])",
    re.IGNORECASE | re.MULTILINE,
)
#: Markdown emphasis and code marks a model wraps around a key or a status.
STEP_MARKUP_RE = re.compile(r"[`*]")
#: The one status that settles a step.
STEP_DONE = "done"
#: How many unfinished steps the refusal names.
MAX_LISTED_STEPS = 10

_CARD_BODY_SQL = "SELECT COALESCE(body, '') FROM tasks WHERE id = ?"
#: The card's assignee's comments in the order they were written; the id
#: breaks a same-second tie. Only the assignee's: the front door comments on a
#: ``needs_input`` card with the user's answer verbatim, so its lines are not
#: the worker's record. Compared the way upstream normalizes a profile name
#: (``normalize_profile_name``: trimmed, lowercase), since the dispatcher sets
#: the worker's ``HERMES_PROFILE``, and so its comments' author, from that.
_CARD_COMMENTS_SQL = (
    "SELECT c.body FROM task_comments c JOIN tasks t ON t.id = c.task_id"
    " WHERE c.task_id = ? AND lower(trim(c.author)) = lower(trim(t.assignee))"
    " ORDER BY c.created_at, c.id"
)

STEPS_REFUSAL = (
    "kanban_complete refused: {count} step(s) this card declared are not "
    "done — {listing}. A card closes `done` only when every step it names "
    "was delivered; completing now would tell the requester it was. Either "
    "finish the step and record it with exactly this line, the key as listed: "
    "kanban_comment(body=\"step {example}: done\"), or end the card with "
    "kanban_block(kind=\"needs_input\") whose `reason` names what was not "
    "delivered and, for anything already created, the resource, its state and "
    "how to remove it. You cannot skip a step on your own: one that turned "
    "out to be unneeded is a block that says so. If the requester has "
    "answered that a step is not wanted, record their answer as its outcome: "
    "kanban_comment(body=\"step {example}: done - dropped at the requester's "
    "request\"). The `result` you submitted has "
    "been saved on this card in the meantime."
)

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

    The single call the patched ``kanban_create`` handler makes, directly
    after the created card is read back and before ``kanban_auto_subscribe``'s
    subscription inheritance: both hook the same upstream line, so whichever
    applier runs last sits first, and the Dockerfile runs this one last. The
    two are order-independent -- each writes its own table and neither reads
    the other's -- and share the same worker test. A process with no
    ``HERMES_KANBAN_TASK`` (chat session, CLI, cron) writes nothing.
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

    ``connect`` is ``hermes_cli.kanban_db_connect.connect`` (injected by the applier's
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


#: Whether a card has a parent that isn't settled: the wait upstream's
#: ``recompute_ready`` actually honours.
_UNSETTLED_PARENT_SQL = (
    "SELECT 1 FROM task_links l JOIN tasks t ON t.id = l.parent_id"
    f" WHERE l.child_id = ? AND t.status NOT IN {SETTLED_STATUSES!r} LIMIT 1"
)


def _children_the_repair_will_flip(conn, task_id: str) -> list:
    """The children ``kanban_scheduling``'s repair turns into parents of
    ``task_id`` inside ``block_task``, after this gate runs. Installed as
    ``hermes_cli/kanban_scheduling.py`` in the image; ``kanban_scheduling``
    beside this file on a host. Raises if neither imports, which the gate
    answers by letting the block through."""
    try:
        from hermes_cli.kanban_scheduling import find_deadlocked_children
    except ImportError:
        from kanban_scheduling import find_deadlocked_children
    return find_deadlocked_children(conn, task_id)


DEPENDENCY_REFUSAL = (
    "kanban_block refused: kind=\"dependency\" waits for this card's parent "
    "cards, and it has none left unfinished, so the board would hand this "
    "card straight back to a NEW worker within seconds. That worker can't see "
    "what you've already done (a pull request you opened, a change you made) "
    "and would start the task over. You still hold the card. "
    "To wait for cards you fanned out{listing}: keep polling each with "
    "kanban_show(<child id>), run `sleep 60` between polling rounds, and call "
    "kanban_complete with the synthesized answer once they're settled. "
    "If a child is blocked or keeps failing, use "
    "kanban_block(kind=\"needs_input\") naming it, or complete with the "
    "answer you have and say plainly which part is missing and why. "
    "A card that lists this one in its `parents` runs only after you "
    "finish, so don't wait for it: complete this card and it starts."
)


def refuse_idle_dependency_block(task_id, kind, conn, run_id=None) -> str | None:
    """The block gate. ``None`` when the block may proceed.

    ``conn`` is the board connection the ``kanban_block`` handler already has
    open; it is read, never written or closed. Only ``kind="dependency"`` is
    considered, and only a card with nothing unfinished to wait on is refused
    (see the module docstring). A card that isn't this worker's running card
    any more (its claim was released or reassigned; ``run_id`` is the
    worker's dispatcher run, when known) is left to upstream's ``block_task``,
    which reports that the block didn't land. Fails open on any error.
    """
    if kind != "dependency":
        return None
    key = str(task_id or "").strip()
    if not key:
        return None
    try:
        card = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (key,)
        ).fetchone()
        if card is None or card[0] != "running":
            return None
        if run_id is not None and card[1] is not None and int(card[1]) != int(run_id):
            return None
        if conn.execute(_UNSETTLED_PARENT_SQL, (key,)).fetchone():
            return None
        if _children_the_repair_will_flip(conn, key):
            return None
        try:
            rows = conn.execute(_LIVE_CHILDREN_SQL, (key,)).fetchall()
        except Exception:  # noqa: BLE001 — a board without the table
            rows = []
    except Exception as exc:  # noqa: BLE001 — advisory gate, never block
        logger.warning(
            "kanban_children_settled: could not check what %s's dependency "
            "block would wait on (allowing the block): %r",
            key, exc,
        )
        return None
    children = [(str(r[0]), str(r[1])) for r in rows]
    listing = ""
    if children:
        listing = " (" + ", ".join(
            f"`{cid}` ({status})" for cid, status in children[:MAX_LISTED_CHILDREN]
        )
        overflow = len(children) - MAX_LISTED_CHILDREN
        if overflow > 0:
            listing += f" and {overflow} more"
        listing += ")"
    logger.warning(
        "kanban_children_settled: refused a dependency block on %s, which "
        "has nothing unfinished to wait on (%d live fan-out child(ren))",
        key, len(children),
    )
    return DEPENDENCY_REFUSAL.format(listing=listing)


def card_steps(conn, task_id: str) -> dict[str, str]:
    """Each declared step's latest status, by key: the card's body first,
    then its comments in the order they were written."""
    steps: dict[str, str] = {}
    row = conn.execute(_CARD_BODY_SQL, (task_id,)).fetchone()
    texts = [row[0] if row else ""]
    texts += [r[0] or "" for r in conn.execute(_CARD_COMMENTS_SQL, (task_id,)).fetchall()]
    for text in texts:
        for key, status in STEP_LINE_RE.findall(STEP_MARKUP_RE.sub("", str(text))):
            steps[key.lower()] = status.lower()
    return steps


def require_steps_settled(task_id, connect, result=None) -> str | None:
    """The step gate. ``None`` when the completion may proceed.

    ``connect`` and ``result`` are as for :func:`require_children_settled`,
    and the submitted result is stashed before any refusal the same way. A
    card with no step line completes as before. Not rationed (see the module
    docstring). Fails open on any error.
    """
    key = str(task_id or "").strip()
    if not key:
        return None
    try:
        conn = connect()
        try:
            unfinished = [
                (step, status)
                for step, status in card_steps(conn, key).items()
                if status != STEP_DONE
            ]
            if not unfinished:
                return None
            _stash_submitted_result(conn, key, result)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — advisory gate, never block
        logger.warning(
            "kanban_children_settled: could not check %s's declared steps or "
            "preserve its submitted result (allowing the completion): %r",
            key, exc,
        )
        return None
    listing = ", ".join(
        f"{step} ({status})" for step, status in unfinished[:MAX_LISTED_STEPS]
    )
    overflow = len(unfinished) - MAX_LISTED_STEPS
    if overflow > 0:
        listing += f" and {overflow} more"
    logger.warning(
        "kanban_children_settled: refused completing %s with %d step(s) not done",
        key, len(unfinished),
    )
    return STEPS_REFUSAL.format(count=len(unfinished), listing=listing, example=unfinished[0][0])
