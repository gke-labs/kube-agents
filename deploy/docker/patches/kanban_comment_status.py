"""Tell ``kanban_comment`` what the card it just wrote to is actually doing.

Installed into the image at ``/opt/hermes/tools/kanban_comment_status.py`` and
wired into ``tools/kanban_tools.py`` (the ``kanban_comment`` handler) by
``deploy/docker/patches/apply_kanban_comment_status.py``.

Why
---
On 2026-08-08 task ``t_a8f58a2a`` completed at 19:09:43 and its 6,191-character
report reached the user's Slack thread one second later. At 19:11:30 the front
door called ``kanban_comment`` on that same card and told the user "You'll see
the results post here as soon as the agent completes." The user waited 9m46s for
an answer that was already sitting above the question.

The comment call succeeded. It was unkeepable three ways over and upstream's
return value mentioned none of them:

1. **A comment does not respawn a card.** ``task_runs`` for ``t_a8f58a2a`` still
   held only run 97, and no new card was created between 19:11 and 19:24.
2. **The subscription was already gone.** ``gateway/kanban_watchers.py`` deleted
   a card's ``kanban_notify_subs`` rows on the tick that carried its terminal
   event (``task_terminal = task and task.status in {"done", "archived"}`` →
   ``_kanban_unsub``), so nothing could fire for that card again. As of Hermes
   v2026.8.13 this leg no longer holds for ``done``: upstream narrowed the test
   to ``task.status == "archived"`` on the grounds that ``done`` is reversible,
   and added a periodic sweep for the rows that now survive it. Legs 1 and 3 are
   untouched by that and are what the incident actually turned on — a surviving
   subscription delivers nothing when nothing will ever complete again — so this
   patch still treats ``done`` as undeliverable. See ``TERMINAL_STATUSES``.
3. **Nobody ever re-read the comment.** Comments reach a worker two ways —
   ``build_worker_context`` folds them into the *next* worker's system prompt,
   and ``inject_new_comments_from_env`` (``run_agent.py``) steers them into a
   worker that is running right now. A ``done`` card has neither.

There is also a fourth thing that is true of *every* card, not just closed ones,
and is worth saying because it is the belief that produced the sentence:
**commenting never sends a chat message.** ``add_comment`` appends a
``commented`` event, and ``commented`` is not in the notifier's ``TERMINAL_KINDS``
(``completed, blocked, gave_up, crashed, timed_out, status, archived, unblocked,
block_loop_detected``). A comment can only reach a human by causing a worker to
run and *that* worker to complete.

What upstream returns is ``_ok(task_id=tid, comment_id=cid)`` —
``{"ok": true, "task_id": ..., "comment_id": ...}``. Read by a model that has
just written "here is the question, please answer in this thread", ``ok: true``
means the question was asked. It wasn't.

What this adds
--------------
Three fields, all additive; ``ok``, ``task_id`` and ``comment_id`` keep their
upstream meaning and shape because the model parses them.

``task_status``
    The card's raw ``tasks.status``. The single fact that makes the promise
    obviously wrong: you cannot read ``"task_status": "done"`` and still believe
    an answer is coming.

``comment_reaches``
    A sentence, not a token, because the token would need a legend the model
    does not have: who will read this comment. ``running`` cards are steered
    live, open cards wait for their next worker, terminal cards have neither.

``completion_posts_to_chat``
    Whether the card has a live ``kanban_notify_subs`` row — i.e. whether this
    card completing will post anything into a chat thread at all. This is the
    proposition the incident's sentence actually asserted, so it is worth
    answering directly. Emitted **only for non-terminal cards**; see below.

and, for terminal cards only, ``note``: an explicit instruction not to promise
delivery, plus where the answer already is.

Success, not an error
---------------------
Commenting on a closed card is a legitimate thing to do — annotating a finished
audit, recording an RCA note against a closed incident, leaving provenance on a
card someone will read on the dashboard. Refusing it would break that, and it
would break it in the direction this patch is least allowed to fail: an error
returns without writing, so the comment is *lost*. Losing a comment is a worse
outcome than the incident, which lost only a claim about a comment.

A refusal is also the wrong shape for the actual defect. The write was never the
problem; the write is fine and durable. The problem is that a bare ``ok: true``
licensed an inference — "asked, therefore an answer is coming" — that nothing in
the system supports. The fix is to make that inference unavailable, which a
returned status does and an error only does by accident, at the cost of also
destroying the legitimate call. So: always write, always report.

The one thing the return value must not do is read as pending, which is why the
terminal branch carries an imperative ``note`` rather than leaving the model to
infer the consequence of ``"done"`` on its own — inferring it is exactly what it
failed to do the first time.

Status outranks the subscription row
------------------------------------
``comment_reaches`` is derived from ``task_status`` alone, never from the
subscription count, and ``completion_posts_to_chat`` is withheld on terminal
cards. The two can disagree for one notifier tick: the unsub happens when the
notifier *processes* the terminal event, so a card that completed a second ago
is ``done`` with its rows still present. That window is precisely the incident's
window — 19:09:43 to 19:11:30 is well inside a five-second tick's blast radius
only if the tick stalls, but the ordering hazard is real and permanent. A model
handed ``"task_status": "done"`` next to ``"completion_posts_to_chat": true``
would resolve the contradiction in the optimistic direction, which is the bug.
So the racy field is simply not emitted where it could contradict the durable
one.

Fails toward upstream
---------------------
Every lookup here is wrapped. If ``get_task`` raises, if the row is missing, if
the board predates ``kanban_notify_subs``, :func:`delivery_fields` returns
``{}`` and the handler returns byte-for-byte what upstream returned. The comment
is already committed by the time this runs — ``add_comment`` opens and closes
its own ``write_txn`` — so there is no path on which reporting the status can
cost the caller the comment. Omitting a status field is a regression to previous
behaviour; failing the call would be a new way to lose data.

Cost
----
One extra ``SELECT * FROM tasks WHERE id = ?`` on the connection the handler
already holds open, against the primary key, plus one ``SELECT 1 FROM
kanban_notify_subs WHERE task_id = ? LIMIT 1`` served by ``idx_notify_task``.
The task row is *not* already loaded on this path — upstream's ``add_comment``
checks existence with its own ``SELECT 1`` inside ``write_txn`` and returns only
``lastrowid`` — so the status genuinely costs a query. Two indexed point lookups
against a local SQLite file, on a path that has just done a write transaction,
is not a cost worth trading a silent false promise for.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Statuses on which a comment has no reader: no worker is scheduled to run the
#: card, so neither ``build_worker_context`` nor ``inject_new_comments_from_env``
#: will ever hand the text to one.
#:
#: This used to be defined as the notifier's own ``task_terminal`` set and was
#: kept equal to it. v2026.8.13 narrowed that set to ``{"archived"}`` — ``done``
#: is reversible, so upstream now keeps the subscription alive across it — and
#: the two questions came apart. "Is the chat subscription gone" is upstream's;
#: "will anything read this comment" is this one, and it is the one the returned
#: fields answer. A ``done`` card keeps its subscription and still has no
#: scheduled worker, so it stays here.
#:
#: The coupling that remains is one-directional and is what
#: ``verify_kanban_comment_status.py`` asserts: every status the notifier
#: unsubscribes on must be in this set. Upstream narrowing leaves this patch
#: strictly more conservative, which is the safe direction; upstream *widening*
#: past this set would mean calling a card deliverable after its chat was torn
#: down, which is the incident again on a new status.
TERMINAL_STATUSES = frozenset({"done", "archived"})

#: The status on which a comment reaches the worker *now*, via
#: ``inject_new_comments_from_env`` (``run_agent.py``) steering it into the live
#: agent rather than waiting for ``build_worker_context`` to fold it into the
#: next run's system prompt.
RUNNING_STATUS = "running"

READER_RUNNING = "the worker running this card right now"
READER_NEXT = "the next worker to run this card"
READER_NONE = "nobody"

#: Written at the model, not at a log reader. It has to survive being skimmed,
#: so it leads with the refutation rather than the explanation, and it names the
#: two tools that do what the caller was actually trying to do.
#:
#: It used to carry a third clause — that the card's chat subscription was
#: closed when the card finished. That is the module docstring's leg 2, and
#: v2026.8.13 ended it for ``done``: the subscription now survives, so the note
#: would have handed the model a false premise on every ``done`` card, which is
#: the one thing this module exists to stop doing. Dropped rather than made
#: conditional on the status, because the clause was never load-bearing:
#: "commenting never sends a chat message on any card" is strictly stronger,
#: true of every card, and already says everything the subscription clause said
#: about whether this comment reaches a human.
TERMINAL_NOTE_TEMPLATE = (
    "This card is already {status}, so nothing will happen because of this "
    "comment: no worker is scheduled to read it, and commenting never sends a "
    "chat message on any card. Do NOT say that results will follow from this "
    "comment, and do not wait for them. The card's answer already exists — read "
    "it with kanban_show({task_id!r}) and reply with it yourself. If new work "
    "is needed, create a new card with kanban_create."
)


def _status_of(kb: Any, conn: Any, task_id: Any) -> str:
    """The card's ``status``, or ``""`` when it cannot be established.

    Separate from :func:`delivery_fields` so the empty string has exactly one
    meaning at the call site — "say nothing" — whether the cause was a missing
    card, a missing column, or a raising driver.
    """
    task = kb.get_task(conn, task_id)
    if task is None:
        return ""
    return str(getattr(task, "status", "") or "").strip()


def _has_chat_subscriber(conn: Any, task_id: Any) -> bool:
    """Whether any gateway source is subscribed to this card's events.

    A direct ``SELECT 1 … LIMIT 1`` rather than ``kb.list_notify_subs``: the
    caller only needs existence, and ``list_notify_subs`` builds a dict per row
    and JSON-decodes ``delivery_metadata`` on each one to answer it. Served by
    ``idx_notify_task``.
    """
    row = conn.execute(
        "SELECT 1 FROM kanban_notify_subs WHERE task_id = ? LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def delivery_fields(kb: Any, conn: Any, task_id: Any) -> dict:
    """Extra ``kanban_comment`` result fields describing the card's state.

    Returns a mapping to splat into upstream's ``_ok(...)``. Never raises and
    never returns a key whose value it could not establish: ``{}`` means the
    handler answers exactly as unpatched Hermes did.

    See the module docstring for why a terminal card gets a ``note`` instead of
    an error, and why ``completion_posts_to_chat`` is withheld there.
    """
    try:
        status = _status_of(kb, conn, task_id)
        if not status:
            # No card, or a row without a usable status. add_comment would have
            # raised ValueError("unknown task") before we got here, so this is
            # a shape change rather than a missing card — either way, guessing
            # is worse than staying quiet.
            return {}

        if status in TERMINAL_STATUSES:
            return {
                "task_status": status,
                "comment_reaches": READER_NONE,
                "note": TERMINAL_NOTE_TEMPLATE.format(
                    status=status, task_id=str(task_id),
                ),
            }

        fields = {
            "task_status": status,
            "comment_reaches": (
                READER_RUNNING if status == RUNNING_STATUS else READER_NEXT
            ),
        }
        try:
            fields["completion_posts_to_chat"] = _has_chat_subscriber(
                conn, task_id,
            )
        except Exception as exc:  # noqa: BLE001 — a legacy board has no table
            # Older boards predate kanban_notify_subs entirely (kanban_db's
            # count_notify_subs documents the same case). The status is the
            # load-bearing half; drop only the half that failed.
            logger.debug(
                "kanban_comment_status: subscription probe for %r failed "
                "(continuing without it): %r",
                task_id, exc,
            )
        return fields
    except Exception as exc:  # noqa: BLE001 — never fail a written comment
        logger.warning(
            "kanban_comment_status: status lookup for %r failed "
            "(comment already committed, returning upstream fields): %r",
            task_id, exc,
        )
        return {}
