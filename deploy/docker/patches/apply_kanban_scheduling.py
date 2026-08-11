#!/usr/bin/env python3
"""Wire the kanban scheduling repairs into ``hermes_cli/kanban_db.py``.

Six anchored edits in one file plus a single import trailer. All six used to be
three separate appliers (``apply_kanban_dependency_repair``,
``apply_kanban_claim_fencing``, ``apply_kanban_breaker_counter``) rewriting the
same source in sequence, each with its own idempotency story and two of them
with their own trailer. They are merged because the file is the unit of risk: an
anchor invalidated by a Hermes bump has to be re-derived against whatever the
other five edits left behind, and that is only checkable when they are applied
together and gated together. Behaviour is unchanged by the merge itself; the
discriminator in edit 2 is the one deliberate behaviour change, and it is
documented in ``kanban_scheduling.py``.

    1. ``block_task`` dependency branch — call ``repair_inverted_dependencies``
       immediately before the ``dependency_wait`` event. That point is only
       reached once the status UPDATE has taken (``cur.rowcount != 1`` returns
       early above it), so a block that was a no-op never touches the link
       graph.
    2. ``detect_crashed_workers`` head — sweep dead owners' claims back to
       ``ready`` first, then adjudicate worker PIDs only for claims this exact
       process made, rather than for anything sharing the pod name. The sweep is
       handed ``_resolve_crash_grace_seconds`` so it applies the same
       launch-window grace as the per-row loop it runs ahead of.
    3. ``detect_crashed_workers`` tail — charge the swept cards the sweep
       classified as faults, after its transaction has closed and outside the
       ``crash_details`` fingerprint pass.
    4. ``_record_task_failure`` trip branch — persist the exhausted budget
       rather than the raw attempt count.
    5. ``_record_task_failure`` spawn-path bind — use the floored value.
    6. ``_record_task_failure`` crash-path bind — use the floored value.

Edits 2 and 3 must stay ordered before 4-6 for reading rather than for
correctness: the fence decides which cards reach the breaker at all, and the
charge it adds is a caller of the very function 4-6 rewrite. The anchors do not
overlap.

``_error_fingerprint`` IS NOT PATCHED, and that is a decision rather than an
omission. An earlier version replaced upstream's
``re.sub(r'\\bpid \\d+\\b', 'pid N', ...)`` with a bare ``error_text[:80]``. Every
error text that reaches the fingerprint is built by ``detect_crashed_workers``
itself and every one of them is PID-prefixed, so keeping the PID gave each
worker its own bucket, ``_fp_counts`` could never reach the ``>= 3`` the
systemic heuristic tests, and the detector that halts a board where everything
is failing identically stopped firing at all. Edit 2 removes the cause that edit
was reaching for.

The other three ``host_prefix`` comparisons (``release_stale_claims``,
``_terminate_worker``, ``enforce_max_runtime``) are deliberately left alone.
Narrowing them is not locally safe: ``_terminate_worker`` reports
``host_local: False`` by returning a never-attempted termination, which
``_worker_survived_termination`` then has to interpret, and
``release_stale_claims`` uses the same flag to choose between extending and
reclaiming. Edit 2 makes those paths near-unreachable for foreign claims anyway
— dead owners are handed back long before their 900s TTL — and the 1-hour
``last_heartbeat_at`` backstop still bounds the residual PID-collision case.

WHY EDITS 4-6 EXIST (the breaker counter)

``dispatch_once`` runs ``detect_crashed_workers`` and then, twenty lines later,
``recompute_ready(conn, failure_limit=failure_limit)``. Those two functions each
decide whether a card is over its retry budget, and they do not use the same
threshold.

``_record_task_failure`` trips on ``if force_trip or failures >= effective_limit``
and persists ``consecutive_failures = old + 1`` -- the raw attempt count. Two
callers reach that branch with a threshold the dispatcher never sees:

* the clean-exit protocol-violation path passes ``force_trip=True`` after
  adjudicating its own violation streak against ``_PROTOCOL_VIOLATION_FAILURE_LIMIT``
  (3, or the card's ``max_retries``). A below-budget violation deliberately does
  not tick the unified counter, so a card arriving here normally has
  ``consecutive_failures == 0`` and leaves with 1.
* the systemic same-error path passes ``failure_limit=1``, so it trips at 1.

``recompute_ready`` then re-derives the verdict from ``consecutive_failures``
alone, against ``max_retries`` or the dispatcher's ``failure_limit`` (2 by
default), sees 1 < 2, and executes ``UPDATE tasks SET status = 'ready'`` plus a
``promoted`` event. The next lines of the same ``dispatch_once`` claim and spawn
the card. The block lasts zero ticks: a policy reading "stop after 3 protocol
violations and hand to a human" actually stops after 4.

It does not add a second source of truth. The obvious fix -- have
``recompute_ready`` read the ``gave_up`` event the breaker just wrote -- was
built and rejected: ``assign_task`` zeroes ``consecutive_failures`` on a profile
change (its comment calls reassignment "an explicit recovery action") and emits
kind ``assigned``, not ``unblocked``, without changing status. An event-stickiness
fix short-circuits on the event before the zeroed counter is consulted and pins
the reassigned card in ``blocked`` forever. It also has to ``json.loads`` a
nullable ``payload`` column inside a write transaction, and to treat
``task_events.id`` as a durable clock that ``_rebuild_drifted_tables``
reassigns.

Instead: keep the counter as the single source of truth and store the right
number in it. When the breaker trips, the budget IS exhausted, so persist the
exhausted budget rather than the raw attempt count -- the threshold the trip was
decided against, and, when there is no per-task override, never below
``DEFAULT_FAILURE_LIMIT``, which is the floor of what the reader will test.

Because the counter stays the arbiter, every counter-zeroing recovery path keeps
working untouched: ``unblock_task``, ``complete_task`` via
``_clear_failure_counter``, ``assign_task`` when it changes the assignee, and
``reassign_task``. (``reclaim_task`` is NOT one of them and never was -- it
bails unless the card is ``running`` or still holds a ``claim_lock``, and the
trip cleared both.) Note that ``assign_task`` to the SAME profile is a no-op on
the counter, so post-patch it no longer frees a tripped card; reassign to a
different profile, or use ``hermes kanban unblock``. A card blocked purely by a
parent dependency never trips, so it still auto-recovers. ``gave_up`` does not
become sticky.

The ``gave_up`` payload is not touched: ``payload["failures"]`` still reports the
true attempt count. Reconstructing the stored counter from that payload needs
the floor too, because the floor is invisible in it::

    stored = max(failures, effective_limit)                    # limit_source == "task"
    stored = max(failures, effective_limit, DEFAULT_FAILURE_LIMIT)   # otherwise

``limit_source == "task"`` is exactly ``task_override is not None``. A systemic
trip is the arm that makes the naive one-line formula wrong: it stores 2 while
its payload says ``{"failures": 1, "effective_limit": 1}``.

KNOWN LIMIT: ``detect_crashed_workers`` never sees the dispatcher's configured
``failure_limit``, so the floor makes the two derivations agree under the shipped
configuration (``kanban.failure_limit`` unset, ``DEFAULT_FAILURE_LIMIT = 2``).
Raise it and the trips become revertible again in order: a systemic trip (stored
2) once ``kanban.failure_limit`` exceeds 2, and a protocol trip (stored 3) once
it exceeds 3. Closing that needs the limit plumbed into
``detect_crashed_workers``, which is a larger change to the function edits 2 and
3 already touch.

BEHAVIOUR CHANGE: a card that carries a breaker trip AND an undone parent no
longer auto-promotes when the parent completes. That is the same guard #35072
already applies to normally-tripped cards; a force-tripped card is by definition
one whose budget was declared exhausted. Expect more cards to sit in ``blocked``
awaiting a human. ``kanban_diagnostics._rule_repeated_failures`` (threshold 3)
surfaces the protocol-violation case.

COMPATIBILITY WITH ``apply_kanban_wake_nudge``

That patch edits this same file, at ``create_task``, ``complete_task`` and
``unblock_task``, and runs after this one. None of the six anchors here fall in
those functions and none of the replacement text contains any of its three
anchor strings, so this applier neither consumes nor invalidates them.
``test_kanban_scheduling.py`` asserts that rather than leaving it to inspection,
because the coupling is invisible from either file alone.

Usage::

    python3 apply_kanban_scheduling.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "hermes_cli/kanban_db.py"

# Build marker the Dockerfile greps for after the breaker edits.
BUILD_MARKER = "persisted_failures = max(failures, effective_limit)"

# Written by every successful apply and by nothing else. Checked before any
# edit, because three of the six anchors survive their own replacement (the
# patched text keeps the anchor and inserts around it), so counting alone waves
# a re-run straight through: replayed against the running gateway's kanban_db.py
# the unguarded dependency applier exited 0 three times and left three copies of
# the call and three trailer imports behind.
ALREADY_PATCHED = (
    "from hermes_cli.kanban_scheduling import",
    "_kanban_repair_inverted_deps(conn, task_id, reason)",
)

# --- 1. block_task: repair inverted fan-out edges before declaring the wait ---

DEPENDENCY_ANCHOR = (
    '            _append_event(\n'
    '                conn, task_id, "dependency_wait",\n'
    '                {"reason": reason, "kind": kind}, run_id=run_id,\n'
)

DEPENDENCY_PATCHED = (
    '            # kube-agents patch: a card that waits on cards which list *it*\n'
    '            # as their parent has deadlocked — claim_task refuses them until\n'
    '            # this card is done, and this card is waiting on them. Invert\n'
    '            # those edges so they dispatch now and this card resumes when\n'
    '            # they finish. See hermes_cli/kanban_scheduling.py.\n'
    '            _kanban_repair_inverted_deps(conn, task_id, reason)\n'
) + DEPENDENCY_ANCHOR

# --- 2. detect_crashed_workers: fence the liveness check to this process ------

FENCE_ANCHOR = (
    '        rows = conn.execute(\n'
    '            "SELECT id, worker_pid, claim_lock, started_at FROM tasks "\n'
    '            "WHERE status = \'running\' AND worker_pid IS NOT NULL"\n'
    '        ).fetchall()\n'
    '        host_prefix = f"{_claimer_id().split(\':\', 1)[0]}:"\n'
    '        for row in rows:\n'
    '            # Only check liveness for claims owned by this host.\n'
    '            lock = row["claim_lock"] or ""\n'
    '            if not lock.startswith(host_prefix):\n'
    '                continue\n'
)

FENCE_PATCHED = (
    '        # kube-agents patch: under Kubernetes the host half of the claim\n'
    '        # token is the pod name, so it survives a container restart even\n'
    '        # though every process that wrote it is gone. Hand back whatever a\n'
    '        # dead owner still holds, then adjudicate PIDs only for claims this\n'
    '        # exact process made. The sweep gets the same launch-window grace\n'
    '        # period as the per-row loop below; what it hands back is split into\n'
    '        # faults and infrastructure and charged after this transaction\n'
    '        # closes. See hermes_cli/kanban_scheduling.py.\n'
    '        _kanban_claimer = _claimer_id()\n'
    '        _kanban_reclaimed = _kanban_release_dead_foreign_claims(\n'
    '            conn, _kanban_claimer, _pid_alive, _resolve_crash_grace_seconds\n'
    '        )\n'
    '        rows = conn.execute(\n'
    '            "SELECT id, worker_pid, claim_lock, started_at FROM tasks "\n'
    '            "WHERE status = \'running\' AND worker_pid IS NOT NULL"\n'
    '        ).fetchall()\n'
    '        for row in rows:\n'
    '            # Only check liveness for claims made by this process life.\n'
    '            lock = row["claim_lock"] or ""\n'
    '            if not _kanban_claim_is_self(lock, _kanban_claimer):\n'
    '                continue\n'
)

# --- 3. detect_crashed_workers: charge the reclaims that are faults -----------
#
# The tail of the function: past the ``crash_details`` accounting loop, so
# ``auto_blocked`` exists and the sweep's transaction is long closed, and before
# the side-channel stash that publishes it to ``dispatch_once``.
CHARGE_ANCHOR = (
    "    # Stash auto-blocked ids on the function for the dispatch loop to pick up.\n"
)

CHARGE_PATCHED = (
    "    # kube-agents patch: a card the sweep handed back ran and produced\n"
    "    # nothing, which is a failed run whoever's claim died -- and left\n"
    "    # uncharged it is an unbounded one, because a card that kills the\n"
    "    # dispatcher is reclaimed by the replacement process and dispatched\n"
    "    # again with its counter still at zero. Only ``chargeable`` is spent:\n"
    "    # the sweep forgives a release whose owner was a pod Kubernetes\n"
    "    # replaced, so an ordinary rollout does not cost every in-flight card a\n"
    "    # retry. Charged here rather than inside the sweep because\n"
    "    # ``_record_task_failure`` opens its own ``write_txn``, and in its own\n"
    "    # loop rather than as ``crash_details`` entries because one event\n"
    "    # releases every in-flight card with one identical error text, which the\n"
    "    # fingerprint pass above would read as systemic and abandon.\n"
    "    # See hermes_cli/kanban_scheduling.py.\n"
    "    auto_blocked.extend(\n"
    "        _kanban_charge_reclaimed_cards(\n"
    "            conn, _kanban_reclaimed.chargeable, _record_task_failure\n"
    "        )\n"
    "    )\n"
    "    # Stash auto-blocked ids on the function for the dispatch loop to pick up.\n"
)

# --- 4. _record_task_failure: store the exhausted budget, not the attempt -----

TRIP_ANCHOR = (
    "        if force_trip or failures >= effective_limit:\n"
    "            # Trip the breaker.\n"
)

TRIP_PATCHED = (
    "        if force_trip or failures >= effective_limit:\n"
    "            # Trip the breaker.\n"
    "            # kube-agents patch: persist the exhausted budget, not the raw\n"
    "            # attempt count. ``recompute_ready`` re-derives this same verdict\n"
    "            # from ``consecutive_failures`` alone, against its own limit, and\n"
    "            # promotes the card straight back to ``ready`` in the same\n"
    "            # ``dispatch_once`` tick whenever the stored count is below that\n"
    "            # limit -- which is exactly what a ``force_trip`` (protocol\n"
    "            # violation, decided against its own streak) or a caller-lowered\n"
    "            # ``failure_limit`` (systemic crash, 1) leaves behind. Flooring\n"
    "            # the stored counter at the threshold this trip was decided\n"
    "            # against makes the two derivations agree with no new state, and\n"
    "            # keeps the counter the single arbiter -- so every path that\n"
    "            # zeroes it (unblock_task, complete_task, reassign_task, and\n"
    "            # assign_task to a DIFFERENT profile) still releases the card.\n"
    "            # reclaim_task does not: it bails unless the card is running or\n"
    "            # still holds a claim_lock, and the trip cleared both.\n"
    "            # See deploy/docker/patches/apply_kanban_scheduling.py.\n"
    "            persisted_failures = max(failures, effective_limit)\n"
    "            if task_override is None:\n"
    "                # No per-task override, so the reader resolves against the\n"
    "                # dispatcher's limit, whose shipped value is the module\n"
    "                # default. Never store below it.\n"
    "                persisted_failures = max(\n"
    "                    persisted_failures, DEFAULT_FAILURE_LIMIT\n"
    "                )\n"
)

# --- 5. the spawn-failure branch bind tuple -----------------------------------
#
# The bind line alone occurs four times in the function (two in this blocked
# branch, two in the below-threshold branch that must keep the raw count), so
# the preceding WHERE clause is load-bearing for uniqueness -- it is what
# distinguishes this branch from its sibling.
SPAWN_BIND_ANCHOR = (
    "                    \"WHERE id = ? AND status IN ('running', 'ready')\",\n"
    "                    (failures, error[:500], task_id),\n"
)

SPAWN_BIND_PATCHED = (
    "                    \"WHERE id = ? AND status IN ('running', 'ready')\",\n"
    "                    (persisted_failures, error[:500], task_id),\n"
)

# --- 6. the timeout/crash branch bind tuple -----------------------------------

CRASH_BIND_ANCHOR = (
    "                    \"WHERE id = ? AND status IN ('ready', 'running')\",\n"
    "                    (failures, error[:500], task_id),\n"
)

CRASH_BIND_PATCHED = (
    "                    \"WHERE id = ? AND status IN ('ready', 'running')\",\n"
    "                    (persisted_failures, error[:500], task_id),\n"
)

TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_scheduling.py\n"
    "from hermes_cli.kanban_scheduling import (  # noqa: E402\n"
    "    charge_reclaimed_cards as _kanban_charge_reclaimed_cards,\n"
    "    claim_is_self as _kanban_claim_is_self,\n"
    "    release_dead_foreign_claims as _kanban_release_dead_foreign_claims,\n"
    "    repair_inverted_dependencies as _kanban_repair_inverted_deps,\n"
    ")\n"
)

EDITS = (
    ("block_task dependency repair", DEPENDENCY_ANCHOR, DEPENDENCY_PATCHED),
    ("detect_crashed_workers fence", FENCE_ANCHOR, FENCE_PATCHED),
    ("reclaim charging", CHARGE_ANCHOR, CHARGE_PATCHED),
    ("_record_task_failure trip floor", TRIP_ANCHOR, TRIP_PATCHED),
    ("spawn-path blocked bind", SPAWN_BIND_ANCHOR, SPAWN_BIND_PATCHED),
    ("crash-path blocked bind", CRASH_BIND_ANCHOR, CRASH_BIND_PATCHED),
)


def apply(root: Path) -> None:
    """Apply all six edits under ``root``, or raise SystemExit with the reason.

    Nothing is written until every anchor has matched exactly once and the
    result parses, so a Hermes bump that moves one anchor leaves the file
    untouched rather than half-patched.
    """
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban-scheduling")
    patch.refuse_if_patched(*ALREADY_PATCHED)
    for label, anchor, patched in EDITS:
        patch.substitute(anchor, patched, label=label)
    patch.append(TRAILER)
    patch.commit(f"{len(EDITS)} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
