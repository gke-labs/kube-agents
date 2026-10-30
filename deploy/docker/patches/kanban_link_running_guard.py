"""Refuse a kanban_link whose child card has already started.

Installed into the image at ``/opt/hermes/tools/kanban_link_running_guard.py``
and wired into the ``kanban_link`` tool handler (``_handle_link`` in
``tools/kanban_tools.py``) by
``deploy/docker/patches/apply_kanban_link_running_guard.py``.

Why
---
In upstream's kanban a ``task_links`` row is a "runs after" edge: the child
can't be claimed until every parent is done (``claim_task``'s
``_parents_satisfied`` gate in ``hermes_cli/kanban_db.py``). ``link_tasks``
demotes a ``ready`` child to ``todo`` and does nothing else, so linking a card
that is already running leaves its run in place but gated behind the new
parent.

On a ``next`` rehearsal install (issue #3248, 2026-10-15) the Platform Agent
fanned out a child with ``kanban_create`` and then called
``kanban_link(parent_id=<its own card>, child_id=<the child>)``. The child was
already running. Its run's ``kanban_complete`` calls were refused, the run
ended as ``timed_out``, the re-claim came back ``claim_rejected
{'reason': 'parents_not_done'}``, and the child only re-ran after the parent
finished. The thread got two answers, the second with no "Late result" lead,
because #2849's fold steps aside for a child its creator has linked.

A link is how you queue work to run after another card, and that only makes
sense before the card starts. On a running or claimed card it strands the live
run. A card that has already had a run is refused too, on purpose: it's work
already under way, a timed-out or crashed run goes back to ``ready`` and is
usually claimed again within seconds, and a link turns a fan-out child into
what #2849's fold reads as a gated continuation. Gating a blocked card behind
a prerequisite is the one thing this gives up, and filing fresh work for it
covers that. So the handler refuses those links, whoever the parent is, and
tells the caller what to do instead: wait for a running child's result, or
leave a child that ran and stopped as it is (a blocked one needs an unblock,
not a link). It doesn't suggest creating a card with ``parents`` instead: a refused model would take that literally and file a second card for
the work the running child is already doing.

What counts as started
----------------------
Upstream's own markers, read through upstream's own helpers:

* ``tasks.status == 'running'`` (``kb.get_task``);
* a ``tasks.claim_lock`` (the claim CAS in ``_claim_and_open_run`` sets it);
* any ``task_runs`` row for the card (``kb.latest_run``), open or closed.

Scope
-----
Only the ``kanban_link`` tool. ``kanban_create``'s ``parents`` argument is
untouched: creating a card with ``parents=[<your card>]`` is how a worker
queues follow-up work, and the Platform SOUL depends on it. A link to a card
that has never started (``todo``, ``ready``, ``triage``, ...) is untouched.

The check and upstream's ``link_tasks`` run in separate transactions
(``link_tasks`` opens its own ``write_txn``, which refuses to nest), so a
dispatcher claim landing in the milliseconds between them is not caught. The
incident is a link made well after the claim.

Fail-open: a board that can't be read lets the link through to upstream,
which reports unknown ids and cycles itself.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: ``tasks.status`` for a card a worker holds (``_claim_and_open_run``).
RUNNING_STATUS = "running"

#: For a child that is claimed or running. Steers the caller to waiting,
#: deliberately not to creating a card with ``parents=[...]``: a refused model
#: takes the suggestion literally and files a second card for the work the
#: running child is already doing.
RUNNING_REFUSAL = (
    "kanban_link refused: {child} is already running and reports back when it "
    "finishes; a link now would make it wait for {parent} and strand its run. "
    "No link is needed: wait for its result."
)

#: For the caller's own card as the child (``HERMES_KANBAN_TASK`` is the
#: child): a worker that wants its card to wait for a helper. The link would
#: make ``complete_task`` fail on its own card mid-run, and the way to wait is
#: to keep the card open and poll the helper, as SOUL section 0 says.
OWN_CARD_REFUSAL = (
    "kanban_link refused: {child} is your own card and it is already running, "
    "so a link would make it wait for {parent} and refuse your own completion. "
    "To wait for {parent}, keep your card open and check it with kanban_show "
    "until it settles."
)

#: For a child with a run on record that isn't running now (blocked, done,
#: back in todo/ready). "Wait for its result" would be wrong here: a blocked
#: card won't move until someone unblocks it.
STARTED_REFUSAL = (
    "kanban_link refused: {child} has already started (status: {status}); a "
    "link now would gate it behind {parent}. Leave its dependencies as they "
    "are; if it is blocked, it needs an unblock, not a link."
)


def started_refusal_template(kb, conn, child_id: str) -> tuple[str, str] | None:
    """``(refusal template, child status)`` for a child that has started, or
    ``None`` when it hasn't. :data:`RUNNING_REFUSAL` when it is running or
    claimed, :data:`STARTED_REFUSAL` when it only has a run on record."""
    task = kb.get_task(conn, child_id)
    if task is None:
        return None
    if task.status == RUNNING_STATUS or task.claim_lock:
        return RUNNING_REFUSAL, task.status
    if kb.latest_run(conn, child_id) is not None:
        return STARTED_REFUSAL, task.status
    return None


def refuse_started_child_link(kb, conn, parent_id: str, child_id: str) -> str | None:
    """The ``kanban_link`` gate. ``None`` when the link may proceed."""
    if not parent_id or not child_id or parent_id == child_id:
        return None  # upstream's own checks answer these
    try:
        if kb.get_task(conn, parent_id) is None:
            return None  # upstream reports the unknown id
        started = started_refusal_template(kb, conn, child_id)
        if started is None:
            return None
    except Exception as exc:  # noqa: BLE001 — fail open, but say so
        logger.warning(
            "kanban_link_running_guard: could not read %s or %s, so the link "
            "%s -> %s is unguarded: %r", parent_id, child_id, parent_id, child_id, exc,
        )
        return None
    template, status = started
    if template == RUNNING_REFUSAL and os.environ.get("HERMES_KANBAN_TASK") == child_id:
        template = OWN_CARD_REFUSAL
    logger.warning(
        "kanban_link_running_guard: refused kanban_link %s -> %s: the child "
        "has already started (status %s)", parent_id, child_id, status,
    )
    return template.format(child=child_id, parent=parent_id, status=status)
