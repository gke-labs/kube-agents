#!/usr/bin/env python3
"""Wire the kanban scheduling repairs into ``hermes_cli/kanban_db.py`` and
``hermes_cli/kanban_db_dispatch.py``.

Six anchored edits across two files plus one import trailer per file. Most of
them used to be three separate appliers (``apply_kanban_dependency_repair``,
``apply_kanban_claim_fencing``, ``apply_kanban_breaker_counter``) rewriting the
same source in sequence, each with its own idempotency story and two of them
with their own trailer. They are merged because the file is the unit of risk: an
anchor invalidated by a Hermes bump has to be re-derived against whatever the
other edits left behind, and that is only checkable when they are applied
together and gated together. Behaviour is unchanged by the merge itself; the
discriminator in edit 3 and the discount in edit 6 are the deliberate behaviour
changes, and both are documented in ``kanban_scheduling.py``.

Hermes v2026.9.14 split ``kanban_db.py`` into an origin module plus
``kanban_db_connect.py`` / ``kanban_db_workspace.py`` / ``kanban_db_dispatch.py``
/ ``kanban_db_notify.py`` / ``kanban_db_graph.py`` (upstream ``e03a680592``). The dispatcher — crash
detection, failure accounting, the concurrency counter — now lives in
``kanban_db_dispatch.py`` and reaches origin helpers late-bound through
``_kb`` (``from hermes_cli import kanban_db as _kb`` at its tail), so that
monkeypatching ``kanban_db.<name>`` keeps working. ``block_task`` stayed in the
origin module. The edits therefore land in two files:

``hermes_cli/kanban_db.py``

    1. ``block_task`` — call ``repair_inverted_dependencies`` immediately before
       the block's event, and only for ``kind == "dependency"``. Upstream folded
       the per-kind routing into ``_route_block`` (``5d2f0f4789``), so the event
       call is now one generic ``_append_event(conn, task_id, event_kind, ...)``
       for every kind; the ``kind == "dependency"`` guard is the same test
       upstream applies two lines further down. That point is only reached once
       the status UPDATE has taken (``rowcount != 1`` returns early above it),
       so a block that was a no-op never touches the link graph.

``hermes_cli/kanban_db_dispatch.py``

    2. ``_CrashSweep`` — one more field, ``reclaimed_chargeable``. Upstream
       split ``detect_crashed_workers`` into ``_reclaim_dead_workers`` (the
       reclaim transaction, returning this dataclass) and ``_account_crashes``
       (``e66429eb0a``), so what the sweep hands back has to ride out of the
       transaction on the object that already carries ``crash_details``.
    3. ``_reclaim_dead_workers`` head — sweep dead owners' claims back to
       ``ready`` first, then adjudicate worker PIDs only for claims this exact
       process made, rather than for anything sharing the pod name. The sweep is
       handed ``_kb._resolve_crash_grace_seconds`` so it applies the same
       launch-window grace as the per-row loop it runs ahead of, and
       ``_kb._pid_alive`` so it reads liveness from the same late-bound name
       that loop does.
    4. ``detect_crashed_workers`` — charge the swept cards the sweep classified
       as faults, after ``_reclaim_dead_workers`` has returned (its transaction
       is closed) and after ``_account_crashes`` has run (so the reclaims never
       enter the fingerprint pass).
    5. ``_record_task_failure`` trip branch — persist the exhausted budget
       rather than the raw attempt count. Upstream merged the spawn-path and
       crash-path trip UPDATEs into one statement with a conditional fragment,
       and hoists ``error = error[:500]`` to the top of the function, so what
       used to be edits 4-6 (the floor plus two binds) is one edit: the floor,
       then the single bind it feeds.
    6. ``count_running_tasks`` — discount the ``running`` cards that are only
       waiting for work they fanned out, so a coordinator does not hold the slot
       its own children need (issue #1252). At the counter rather than its two
       call sites because ``count_running_tasks_other_boards`` calls it per
       board and the cap is host-level. The text is unchanged from v2026.8.19;
       only the file moved.

Edits 2-4 must stay ordered before 5 for reading rather than for correctness:
the fence decides which cards reach the breaker at all, and the charge it adds
is a caller of the very function 5 rewrites. Edit 6 is independent of all of
them. The anchors do not overlap. Both files are held in memory and compiled
before either is written, so a Hermes bump that moves one anchor in the second
file leaves the first untouched rather than half of the patch applied.

``_error_fingerprint`` IS NOT PATCHED, and that is a decision rather than an
omission. An earlier version replaced upstream's
``re.sub(r'\\bpid \\d+\\b', 'pid N', ...)`` with a bare ``error_text[:80]``. Every
error text that reaches the fingerprint is built by ``_classify_dead_worker``
and every one of them is PID-prefixed, so keeping the PID gave each worker its
own bucket, ``fp_counts`` could never reach the ``>= 3`` the systemic heuristic
tests, and the detector that halts a board where everything is failing
identically stopped firing at all. Edit 3 removes the cause that edit was
reaching for.

The other three host-prefix comparisons (``release_stale_claims`` in the origin
module; ``_terminate_reclaimed_worker`` and ``enforce_max_runtime`` in the
dispatcher, all three now through upstream's ``_host_prefix()`` helper) are
deliberately left alone. Narrowing them is not locally safe:
``_terminate_reclaimed_worker`` reports ``host_local: False`` by returning a
never-attempted termination, which ``_worker_survived_termination`` then has to
interpret, and ``release_stale_claims`` uses the same flag to choose between
extending and
reclaiming. Edit 3 makes those paths near-unreachable for foreign claims anyway
— dead owners are handed back long before their 900s TTL — and the 1-hour
``last_heartbeat_at`` backstop still bounds the residual PID-collision case.
Upstream's ``reconcile_orphaned_running`` (new since v2026.8.19) is disjoint
from the sweep: it selects ``running`` rows whose ``claim_lock`` is NULL, and
the sweep requires a non-NULL lock that is not ours.

WHY EDIT 5 EXISTS (the breaker counter)

``dispatch_once`` runs ``detect_crashed_workers`` and then, a few lines later,
``recompute_ready(conn, failure_limit=failure_limit)``. Those two functions each
decide whether a card is over its retry budget, and they do not use the same
threshold.

``_record_task_failure`` trips on ``if force_trip or failures >= effective_limit``
(spelled as the negation guarding the below-threshold early return) and
persists ``consecutive_failures = old + 1`` -- the raw attempt count. Two
callers reach that branch with a threshold the dispatcher never sees:

* the clean-exit protocol-violation path in ``_account_crashes`` passes
  ``force_trip=True`` after adjudicating its own violation streak against
  ``_PROTOCOL_VIOLATION_FAILURE_LIMIT`` (3, or the card's ``max_retries``). A
  below-budget violation deliberately does not tick the unified counter, so a
  card arriving here normally has ``consecutive_failures == 0`` and leaves
  with 1.
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
``detect_crashed_workers``, which is a larger change to the function edits 3 and
4 already touch.

BEHAVIOUR CHANGE: a card that carries a breaker trip AND an undone parent no
longer auto-promotes when the parent completes. That is the same guard #35072
already applies to normally-tripped cards; a force-tripped card is by definition
one whose budget was declared exhausted. Expect more cards to sit in ``blocked``
awaiting a human. ``kanban_diagnostics._rule_repeated_failures`` (threshold 3)
surfaces the protocol-violation case.

COMPATIBILITY WITH ``apply_kanban_wake_nudge``

That patch edits ``kanban_db.py`` too, at ``create_task``, ``complete_task`` and
``unblock_task``, and runs after this one. The one anchor here in that file is
in ``block_task``, its replacement text contains none of the three wake anchors,
and the other five edits are in a file that patch does not touch, so this
applier neither consumes nor invalidates them. ``test_kanban_scheduling.py``
asserts that rather than leaving it to inspection, because the coupling is
invisible from either file alone.

Usage::

    python3 apply_kanban_scheduling.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

# Edit 6 reads a table another patch owns. Both modules declare its name and the
# settled-status set independently — importing across them would put hermes_cli
# on tools/ — so they are reconciled below and a rename in either fails the build
# instead of silently making the discount a constant zero.
import sqlite3  # noqa: E402

import kanban_children_settled as _children  # noqa: E402
import kanban_scheduling as _scheduling  # noqa: E402


def _reconcile_with_the_writer() -> None:
    """Fail the build when edit 6 and the attribution writer have drifted apart.

    Called from ``apply`` rather than run at import. A raise at import time takes
    the whole test module down with it — including the agreement tests written
    for exactly this drift, which then never execute to report it.
    """
    if _scheduling.CHILDREN_TABLE != _children.CHILDREN_TABLE:
        raise SystemExit(
            "apply_kanban_scheduling: CHILDREN_TABLE disagrees between "
            f"kanban_scheduling ({_scheduling.CHILDREN_TABLE!r}) and "
            f"kanban_children_settled ({_children.CHILDREN_TABLE!r}). "
            "Edit 6 would count nothing. Reconcile them before building."
        )
    if tuple(_scheduling.CHILD_SETTLED_STATUSES) != tuple(_children.SETTLED_STATUSES):
        raise SystemExit(
            "apply_kanban_scheduling: the settled-status set disagrees between "
            f"kanban_scheduling ({_scheduling.CHILD_SETTLED_STATUSES!r}) and "
            f"kanban_children_settled ({_children.SETTLED_STATUSES!r}). Edit 6 and "
            "the completion gate would disagree about whether a card is waiting."
        )

    # The table name is not the only way the two can drift. Edit 6's SQL also names
    # two of its columns, and a rename there raises inside count_waiting_on_children,
    # is swallowed by its fail-open, and leaves the discount a constant zero -- the
    # same silent failure, through a door the name check does not cover.
    #
    # The writer's DDL is executed rather than searched, because searching it is the
    # check that looks right and is not: renaming the column in the CREATE TABLE
    # leaves the name behind in the CREATE INDEX, and a substring test waves that
    # through. sqlite answers the question exactly and costs a millisecond.
    probe = sqlite3.connect(":memory:")
    try:
        for ddl in _children._TABLE_DDL:
            probe.execute(ddl)
    except sqlite3.Error as exc:
        raise SystemExit(
            "apply_kanban_scheduling: kanban_children_settled._TABLE_DDL does not "
            f"execute ({exc}). Edit 6 reads the table it creates."
        ) from exc
    writer_columns = {
        row[1]
        for row in probe.execute(f"PRAGMA table_info({_children.CHILDREN_TABLE})")
    }
    probe.close()

    for column in _scheduling.CHILD_COLUMNS:
        if column not in writer_columns:
            raise SystemExit(
                f"apply_kanban_scheduling: edit 6 reads column {column!r}, which "
                f"kanban_children_settled._TABLE_DDL does not create (it makes "
                f"{sorted(writer_columns)}). The discount would fail open to zero "
                "on every board."
            )
        if column not in _scheduling._WAITING_ON_CHILDREN_SQL:
            raise SystemExit(
                f"apply_kanban_scheduling: CHILD_COLUMNS names {column!r} but edit "
                "6's SQL does not use it, so this check is guarding nothing."
            )

    # kanban_scheduling declares the settled set twice -- once as this tuple and once
    # as the SQL fragment part 2 interpolates. Only the tuple is reconciled above, so
    # tie the fragment to it here rather than leaving one of the three copies loose.
    expected_settled_sql = (
        "(" + ", ".join(repr(s) for s in _scheduling.CHILD_SETTLED_STATUSES) + ")"
    )
    if _scheduling.SETTLED != expected_settled_sql:
        raise SystemExit(
            f"apply_kanban_scheduling: kanban_scheduling.SETTLED ({_scheduling.SETTLED!r}) "
            f"no longer spells CHILD_SETTLED_STATUSES ({expected_settled_sql!r}). "
            "The two halves of the file would disagree about what 'settled' means."
        )


# The origin module: ``block_task`` and the graph.
DB_RELATIVE = "hermes_cli/kanban_db.py"

# The dispatcher module upstream split out at v2026.9: crash detection, the
# breaker, the concurrency counter.
DISPATCH_RELATIVE = "hermes_cli/kanban_db_dispatch.py"

# Build marker the Dockerfile greps for after the breaker edit.
BUILD_MARKER = "persisted_failures = max(failures, effective_limit)"

# The same, for edit 6. Named rather than left as a literal in two places: the
# Dockerfile greps for this exact text, and editing WAITING_PATCHED without it
# breaks that grep with no signal in this file. test_kanban_scheduling asserts
# the Dockerfile still greps for it.
WAITING_BUILD_MARKER = (
    "return max(0, running - _kanban_count_waiting_on_children(conn))"
)

# Written by every successful apply and by nothing else, one set per file.
# Checked before any edit, because three of the six anchors survive their own
# replacement (edits 1, 2 and 4 keep the anchor and insert around it), so
# counting alone waves a re-run straight through: replayed against the running
# gateway's kanban_db.py the unguarded dependency applier exited 0 three times
# and left three copies of the call and three trailer imports behind.
DB_ALREADY_PATCHED = (
    "from hermes_cli.kanban_scheduling import",
    "_kanban_repair_inverted_deps(conn, task_id, reason)",
)
DISPATCH_ALREADY_PATCHED = (
    "from hermes_cli.kanban_scheduling import",
    "_kanban_claim_is_self(lock, _kanban_claimer)",
    BUILD_MARKER,
)

# --- 1. block_task: repair inverted fan-out edges before declaring the wait ---
#
# Upstream's ``_route_block`` now hands ``block_task`` the event kind and
# payload for every block kind, so there is one ``_append_event`` call here
# rather than a ``dependency_wait`` literal. The second line pins this call to
# ``block_task`` -- ``get_task`` after an event is how this function, and no
# other, prepares its lifecycle hook.
DEPENDENCY_ANCHOR = (
    "        _append_event(conn, task_id, event_kind, payload, run_id=run_id)\n"
    "        blocked_task = get_task(conn, task_id)\n"
)

DEPENDENCY_PATCHED = (
    '        if kind == "dependency":\n'
    "            # kube-agents patch: a card that waits on cards which list *it*\n"
    "            # as their parent has deadlocked — claim_task refuses them until\n"
    "            # this card is done, and this card is waiting on them. Invert\n"
    "            # those edges so they dispatch now and this card resumes when\n"
    "            # they finish. See hermes_cli/kanban_scheduling.py.\n"
    "            _kanban_repair_inverted_deps(conn, task_id, reason)\n"
) + DEPENDENCY_ANCHOR

# --- 2. _CrashSweep: carry the sweep's verdict out of the transaction --------

SWEEP_FIELD_ANCHOR = (
    "    exited_hook_payloads: list[dict] = field(default_factory=list)\n"
)

SWEEP_FIELD_PATCHED = SWEEP_FIELD_ANCHOR + (
    "    # kube-agents patch: the cards ``release_dead_foreign_claims`` handed\n"
    "    # back whose release is a fault rather than a pod replacement; charged by\n"
    "    # ``detect_crashed_workers`` once this txn has closed, because\n"
    "    # ``_record_task_failure`` opens its own. See hermes_cli/kanban_scheduling.py.\n"
    "    reclaimed_chargeable: list[str] = field(default_factory=list)\n"
)

# --- 3. _reclaim_dead_workers: fence the liveness check to this process ------

FENCE_ANCHOR = (
    '        rows = conn.execute(\n'
    '            "SELECT id, worker_pid, claim_lock, started_at, assignee "\n'
    '            "FROM tasks "\n'
    '            "WHERE status = \'running\' AND worker_pid IS NOT NULL"\n'
    '        ).fetchall()\n'
    '        host_prefix = _kb._host_prefix()\n'
    '        for row in rows:\n'
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
    '        _kanban_claimer = _kb._claimer_id()\n'
    '        _kanban_reclaimed = _kanban_release_dead_foreign_claims(\n'
    '            conn, _kanban_claimer, _kb._pid_alive, _kb._resolve_crash_grace_seconds\n'
    '        )\n'
    '        sweep.reclaimed_chargeable = _kanban_reclaimed.chargeable\n'
    '        rows = conn.execute(\n'
    '            "SELECT id, worker_pid, claim_lock, started_at, assignee "\n'
    '            "FROM tasks "\n'
    '            "WHERE status = \'running\' AND worker_pid IS NOT NULL"\n'
    '        ).fetchall()\n'
    '        for row in rows:\n'
    '            # Only check liveness for claims made by this process life.\n'
    '            lock = row["claim_lock"] or ""\n'
    '            if not _kanban_claim_is_self(lock, _kanban_claimer):\n'
    '                continue\n'
)

# --- 4. detect_crashed_workers: charge the reclaims that are faults ----------
#
# The three lines that open the function body: the sweep's transaction closed
# when ``_reclaim_dead_workers`` returned, and ``_account_crashes`` has run its
# fingerprint pass, so the charge lands after both.
CHARGE_ANCHOR = (
    "    sweep = _reclaim_dead_workers(conn)\n"
    "    # Outside the main txn: account each crash and maybe trip the breaker.\n"
    "    auto_blocked = _account_crashes(conn, sweep.crash_details) if sweep.crash_details else []\n"
)

CHARGE_PATCHED = CHARGE_ANCHOR + (
    "    # kube-agents patch: a card the sweep handed back ran and produced\n"
    "    # nothing, which is a failed run whoever's claim died -- and left\n"
    "    # uncharged it is an unbounded one, because a card that kills the\n"
    "    # dispatcher is reclaimed by the replacement process and dispatched\n"
    "    # again with its counter still at zero. Only ``reclaimed_chargeable`` is\n"
    "    # spent: the sweep forgives a release whose owner was a pod Kubernetes\n"
    "    # replaced, so an ordinary rollout does not cost every in-flight card a\n"
    "    # retry. Charged here rather than inside the sweep because\n"
    "    # ``_record_task_failure`` opens its own ``write_txn``, and in its own\n"
    "    # loop rather than as ``crash_details`` entries because one event\n"
    "    # releases every in-flight card with one identical error text, which the\n"
    "    # fingerprint pass above would read as systemic and abandon.\n"
    "    # See hermes_cli/kanban_scheduling.py.\n"
    "    auto_blocked.extend(\n"
    "        _kanban_charge_reclaimed_cards(\n"
    "            conn, sweep.reclaimed_chargeable, _record_task_failure\n"
    "        )\n"
    "    )\n"
)

# --- 5. _record_task_failure: store the exhausted budget, not the attempt ----
#
# Upstream now trips with one UPDATE whose claim-clearing fragment is
# conditional on ``release_claim``, and has already clipped ``error`` at the top
# of the function, so the floor and the single bind it feeds are one edit. The
# below-threshold branch above it returns early and keeps binding the raw count.
TRIP_ANCHOR = (
    "        # Spawn path (release_claim) is still running and also clears claim\n"
    "        # state; the timeout/crash path already did.\n"
    "        conn.execute(\n"
    "            \"UPDATE tasks SET status = 'blocked', \"\n"
    "            + (\"claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, \"\n"
    "               if release_claim else \"\")\n"
    "            + \"consecutive_failures = ?, last_failure_error = ? \"\n"
    "            \"WHERE id = ? AND status IN ('running', 'ready', 'review')\",\n"
    "            (failures, error, task_id),\n"
    "        )\n"
)

TRIP_PATCHED = (
    "        # kube-agents patch: persist the exhausted budget, not the raw\n"
    "        # attempt count. ``recompute_ready`` re-derives this same verdict\n"
    "        # from ``consecutive_failures`` alone, against its own limit, and\n"
    "        # promotes the card straight back to ``ready`` in the same\n"
    "        # ``dispatch_once`` tick whenever the stored count is below that\n"
    "        # limit -- which is exactly what a ``force_trip`` (protocol\n"
    "        # violation, decided against its own streak) or a caller-lowered\n"
    "        # ``failure_limit`` (systemic crash, 1) leaves behind. Flooring\n"
    "        # the stored counter at the threshold this trip was decided\n"
    "        # against makes the two derivations agree with no new state, and\n"
    "        # keeps the counter the single arbiter -- so every path that\n"
    "        # zeroes it (unblock_task, complete_task, reassign_task, and\n"
    "        # assign_task to a DIFFERENT profile) still releases the card.\n"
    "        # reclaim_task does not: it bails unless the card is running or\n"
    "        # still holds a claim_lock, and the trip cleared both.\n"
    "        # See deploy/docker/patches/apply_kanban_scheduling.py.\n"
    "        persisted_failures = max(failures, effective_limit)\n"
    "        if task_override is None:\n"
    "            # No per-task override, so the reader resolves against the\n"
    "            # dispatcher's limit, whose shipped value is the module\n"
    "            # default. Never store below it.\n"
    "            persisted_failures = max(persisted_failures, DEFAULT_FAILURE_LIMIT)\n"
    "        # Spawn path (release_claim) is still running and also clears claim\n"
    "        # state; the timeout/crash path already did.\n"
    "        conn.execute(\n"
    "            \"UPDATE tasks SET status = 'blocked', \"\n"
    "            + (\"claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, \"\n"
    "               if release_claim else \"\")\n"
    "            + \"consecutive_failures = ?, last_failure_error = ? \"\n"
    "            \"WHERE id = ? AND status IN ('running', 'ready', 'review')\",\n"
    "            (persisted_failures, error, task_id),\n"
    "        )\n"
)

# --- 6. count_running_tasks: a waiter is not occupying a slot ----------------
#
# The whole body, so the replacement cannot land twice and so a Hermes bump that
# rewrites the query fails here rather than leaving the discount applied to a
# count that no longer means what it did.
WAITING_ANCHOR = (
    "    try:\n"
    "        return int(\n"
    "            conn.execute(\n"
    "                \"SELECT COUNT(*) FROM tasks WHERE status = 'running'\"\n"
    "            ).fetchone()[0]\n"
    "        )\n"
    "    except Exception:\n"
    "        return 0\n"
)

WAITING_PATCHED = (
    "    try:\n"
    "        running = int(\n"
    "            conn.execute(\n"
    "                \"SELECT COUNT(*) FROM tasks WHERE status = 'running'\"\n"
    "            ).fetchone()[0]\n"
    "        )\n"
    "    except Exception:\n"
    "        return 0\n"
    "    # kube-agents patch: a card waiting on the work it fanned out is still\n"
    "    # 'running', so it holds a max_in_progress slot its own children need --\n"
    "    # two waiters wedge the shipped cap of 2. Discounted here and not at the\n"
    "    # call sites: count_running_tasks_other_boards calls this per board and\n"
    "    # the cap is host-level. Fails open to no discount.\n"
    "    # See hermes_cli/kanban_scheduling.py (issue #1252).\n"
    f"    {WAITING_BUILD_MARKER}\n"
)

DB_TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_scheduling.py\n"
    "from hermes_cli.kanban_scheduling import (  # noqa: E402\n"
    "    repair_inverted_dependencies as _kanban_repair_inverted_deps,\n"
    ")\n"
)

DISPATCH_TRAILER = (
    "\n\n# kube-agents patch: see hermes_cli/kanban_scheduling.py\n"
    "from hermes_cli.kanban_scheduling import (  # noqa: E402\n"
    "    charge_reclaimed_cards as _kanban_charge_reclaimed_cards,\n"
    "    claim_is_self as _kanban_claim_is_self,\n"
    "    count_waiting_on_children as _kanban_count_waiting_on_children,\n"
    "    release_dead_foreign_claims as _kanban_release_dead_foreign_claims,\n"
    ")\n"
)

# ``(file, label, anchor, replacement)``. The file is part of the edit now that
# there are two of them; the tests iterate this to prove each anchor is
# load-bearing in the file it names.
EDITS = (
    (DB_RELATIVE, "block_task dependency repair", DEPENDENCY_ANCHOR, DEPENDENCY_PATCHED),
    (DISPATCH_RELATIVE, "_CrashSweep reclaim field", SWEEP_FIELD_ANCHOR, SWEEP_FIELD_PATCHED),
    (DISPATCH_RELATIVE, "_reclaim_dead_workers fence", FENCE_ANCHOR, FENCE_PATCHED),
    (DISPATCH_RELATIVE, "reclaim charging", CHARGE_ANCHOR, CHARGE_PATCHED),
    (DISPATCH_RELATIVE, "_record_task_failure trip floor", TRIP_ANCHOR, TRIP_PATCHED),
    (DISPATCH_RELATIVE, "waiting-coordinator discount", WAITING_ANCHOR, WAITING_PATCHED),
)

TRAILERS = (
    (DB_RELATIVE, DB_TRAILER),
    (DISPATCH_RELATIVE, DISPATCH_TRAILER),
)

ALREADY_PATCHED = (
    (DB_RELATIVE, DB_ALREADY_PATCHED),
    (DISPATCH_RELATIVE, DISPATCH_ALREADY_PATCHED),
)


def apply(root: Path) -> None:
    """Apply all six edits under ``root``, or raise SystemExit with the reason.

    Nothing is written until every anchor in both files has matched exactly
    once and both results compile, so a Hermes bump that moves one anchor
    leaves both files untouched rather than one patched and one not.
    """
    _reconcile_with_the_writer()
    patches = {
        relative: patchlib.Patch(root, relative, prefix="kanban-scheduling")
        for relative in (DB_RELATIVE, DISPATCH_RELATIVE)
    }
    for relative, markers in ALREADY_PATCHED:
        patches[relative].refuse_if_patched(*markers)
    for relative, label, anchor, patched in EDITS:
        patches[relative].substitute(anchor, patched, label=label)
    for relative, trailer in TRAILERS:
        patches[relative].append(trailer)
    # ``Patch.commit`` compiles and writes in one step, per file. Compile both
    # here first so the second file failing cannot leave the first on disk.
    for patch in patches.values():
        try:
            compile(patch.source, patch.relative, "exec")
        except SyntaxError as exc:
            raise SystemExit(
                f"kanban-scheduling patch: {patch.relative} no longer parses "
                f"after patching: {exc}"
            )
    edits_per_file = {relative: 0 for relative in patches}
    for relative, _label, _anchor, _patched in EDITS:
        edits_per_file[relative] += 1
    for relative, patch in patches.items():
        patch.commit(f"{edits_per_file[relative]} anchors")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
