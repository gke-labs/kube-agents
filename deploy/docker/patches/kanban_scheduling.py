"""Scheduling repairs for ``hermes_cli/kanban_db.py``.

Three faults share this one upstream file, so they share one patch quartet.
Splitting them across three appliers meant three anchored rewrites of
``kanban_db.py`` racing to stay consistent with each other, two import trailers,
and a build gate whose halves could disagree about what the engine now does.
They are merged here; ``apply_kanban_scheduling.py`` carries the anchors,
``verify_kanban_scheduling.py`` replays all three against a real board inside
the image. The breaker-counter fix (part 3) is a pure source rewrite with no
runtime code, so its reasoning lives in the applier docstring rather than here.

Part 1 — inverted fan-out dependencies (``repair_inverted_dependencies``)
Part 2 — claims fenced to a process life (``release_dead_foreign_claims``)
Part 3 — the breaker counter, in ``apply_kanban_scheduling.py``

===========================================================================
Part 1: repair inverted fan-out dependency edges instead of deadlocking
===========================================================================

Background — the deadlock this exists to break
----------------------------------------------
``task_links(parent_id, child_id)`` is a *predecessor* edge, not a hierarchy.
``claim_task`` enforces it as a structural invariant::

    SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id
     WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1

A card whose parent is not ``done`` can never be claimed. ``recompute_ready``
applies the same rule when promoting ``todo -> ready``, and declines *silently*
— no event, no log line.

Upstream's own contract, from the ``kanban_create`` tool description, is
"create a child of the current one (pass the current task id in ``parents``)
… then **complete your own task**". The fan-out only runs once the creator
completes. That is a legitimate continuation idiom and this patch does not
interfere with it.

The failure is the *other* half. An orchestrator that creates cards with
``parents=[<its own running card>]`` and then calls
``kanban_block(kind="dependency")`` to wait for them has built an unconditional
deadlock: the children cannot start until the parent is done, and the parent
will not finish until the children are. Nothing in the engine detects it.

What that cost us on 2026-08-07
-------------------------------
Card ``t_ab112f5b`` ("Fleet-wide Security Baseline Assessment") created three
per-cluster cards parented to itself and declared ``dependency_wait`` on them
four seconds later. The children logged ``claim_rejected
{"reason": "parents_not_done"}`` and never ran — zero ``task_runs``, no
``started_at``, no ``completed_at``. ``block_task(kind="dependency")`` routes to
``todo`` rather than ``blocked``, which skips the ``block_recurrences`` loop
breaker entirely, so the parent was re-promoted and re-spawned every few
seconds, burning a full agent run each cycle.

The worker eventually diagnosed the deadlock itself, re-created the same work as
three *parentless* cards (which ran fine), and then escaped its own wait by
shelling out::

    python3 -c "import sqlite3; ...
      UPDATE tasks SET status='done', result='Completed by Platform Agent'
       WHERE id IN ('t_d3123d56','t_db5847ee','t_f342ef6d')"

Three cards closed ``done`` with a 27-character fabricated ``result``, no
``completed`` event, and no run row. A second instance (``t_e3089a85``) was
still spinning when this patch was written, with three cluster audit cards
starved in ``todo`` for 15 minutes and counting.

The repair
----------
The agent's intent when it blocks is unambiguous: it wants those cards to run
*now* and wants to resume once they finish. That is precisely the fan-in shape
the engine already supports — it just has the edges pointing the wrong way. So
rather than refusing the block (which leaves the worker to improvise, and we
have seen what it improvises), invert the mis-directed edges:

    delete (self -> child)   and   insert (child -> self)

The children lose the parent that was gating them and get dispatched on the next
tick. The blocking card gains them as genuine prerequisites, so
``recompute_ready`` keeps it in ``todo`` until every one of them is ``done`` —
which is the wait the agent asked for, now enforced by the engine instead of by
a spin loop. No agent cooperation is required and no card is left stranded.

When it is allowed to fire
--------------------------
``kanban_block`` takes ``reason`` and ``kind`` and nothing else, so
``block_task`` never records *what* a card is waiting for and the shape of the
graph is the only evidence there is. "Invert every unsettled successor" reads
that evidence wrong — it rewrites healthy pipelines, which is the bug this
module shipped with and which the last section describes.

What separates the two shapes is *when the edge appeared*. A pipeline's
successor was created by whoever planned the pipeline, before the card that now
blocks had ever been claimed. The deadlocking children were created by this
card, out of a ``kanban_create`` that named it in ``parents``, after it started
running. So an edge is inverted only when the child's ``created`` event is newer
than the blocking card's first ``claimed`` event. ``create_task`` writes the one
and ``claim_task`` / ``reclaim_task`` the other, and ``task_events.id`` is a
single ``AUTOINCREMENT`` sequence for the whole board, so the comparison is an
exact ordering. The obvious alternative — ``tasks.created_at`` against
``tasks.started_at`` — is whole seconds, and a planner laying out a pipeline in
the same second the dispatcher claims its first card is indistinguishable from
the deadlock by those.

The watermark is the *first* claim, the same instant ``claim_task``'s
``started_at = COALESCE(started_at, ?)`` records, not the current run's. A
worker that fans out three cards and is then killed leaves them exactly as
deadlocked as one that blocks; when the card is re-claimed and blocks for real,
those three still need releasing.

A card with no ``claimed`` event has no window at all, the comparison is NULL
and nothing is repaired. That is the right answer for a card someone forced into
``running`` with raw SQL.

The second condition is that inverting must actually free the child: the edge is
turned around only if this card is the *last* unfinished parent gating it. A
child created with two parents is not released by releasing one of them, so
turning that edge around would restructure the graph and change nothing. It also
holds the repair to the flat fan-out we have actually observed, the one shape
where no inversion can change the answer the cycle probe gives for the next
child in the batch.

An edge is also left alone if inverting it would create a cycle (the child
already reaches this card by some other path). Deadlock is preferable to a
corrupt graph, and the caller still gets told about it.

The in-image gate (``verify_kanban_scheduling.py``) drives all of this against
the real engine rather than a mock, because every one of these conditions is
about scheduling behaviour that the schema alone does not show.

The guard that did not work
---------------------------
Until this was measured the first condition was "the blocking card must have no
unsettled parents of its own", on the theory that a card with a live
prerequisite is waiting on *that* and means its block literally. It never
discriminated anything. ``claim_task`` refuses to move a card to ``running``
while any parent is unsettled and ``recompute_ready`` only promotes
``todo -> ready`` once every parent is ``done`` or ``archived``, so every card
``block_task`` will accept has settled parents by construction. Replayed against
the engine on a throwaway board, an ordinary pipeline stage (``A`` done, ``B``
claimed, ``B -> C``) and the ``t_ab112f5b`` deadlock both reported no unsettled
parents — and the guard duly let the pipeline through, inverting ``B -> C`` into
``C -> B`` so ``C`` was dispatched ahead of the stage it exists to follow. A
two-input fan-in went the same way the moment one of its inputs finished.

The only shape that makes the predicate true is ``link_tasks`` attaching a new
parent to an already-running card, and there the card's fan-out children are
still genuinely deadlocked — so suppressing the repair was wrong in that case
too. The guard is deleted rather than repaired.

===========================================================================
Part 2: fence task claims to the process life that made them
===========================================================================

Background — one identity, two lifetimes
----------------------------------------
``_claimer_id()`` builds the lock token that stamps every claimed card::

    return f"{host}:{os.getpid()}"

Under Kubernetes ``socket.gethostname()`` is the **pod name**, so the token is
pod-scoped. ``detect_crashed_workers`` then decides whose worker PIDs it is
entitled to adjudicate using only the host half::

    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    ...
    if not lock.startswith(host_prefix):
        continue

Upstream's docstring says "the whole design is single-host", and on a single
host that holds. In a container it does not: a container restart keeps the pod
name while every process that made those claims is gone. The new process
therefore matches ``host_prefix`` against claims made by its *predecessor* and
runs ``os.kill(pid, 0)`` against PIDs it never issued.

(The pod used to run with ``shareProcessNamespace: true``, so the PID *namespace*
outlived the container — which is why the replacement dispatcher in the incident
below came up as PID 12329 rather than reusing 4. **That is no longer true.** The
operator stopped setting the field, because a shared namespace put the credential
sidecar's ``/proc/<pid>/environ`` inside a directory the sandbox could read; each
container now gets a PID namespace that is torn down with it. So a restart starts
counting from 1 again, and a predecessor's recorded PID — 4, in the transcript
below — is a number the new namespace hands out almost immediately. The
adjudication was never correct either way; what changed is which way it now goes
wrong. See ``release_dead_foreign_claims`` for the consequence.)

What that cost us on 2026-08-07
-------------------------------
The gateway took a ``Fatal Python error: Bus error`` (exit 135) at 04:20:30 and
the container restarted 3 seconds later. At 04:20:51 the fresh dispatcher
adjudicated six in-flight cards claimed by the previous life — every one of them
carrying ``claimer: platform-agent-gateway-75b5f6ddf6-7dkd7:4``, the *old*
dispatcher PID — and marked all six ``crashed`` with ``pid <N> not alive``.

They did not merely retry. ``_error_fingerprint`` normalises the PID out of the
message::

    fp = re.sub(r'\\bpid \\d+\\b', 'pid N', error_text[:80])

so six independent workers killed by one infrastructure event collapsed to a
single fingerprint. Count 6 >= 3 tripped the systemic heuristic, which passes
``failure_limit=1``, so each card gave up on its *first* failure::

    gave_up {"failures": 1, "effective_limit": 1, "limit_source": "dispatcher",
             "error": "pid 1046 not alive", "trigger_outcome": "crashed"}

One gateway crash permanently abandoned every card in flight.

Separately, nothing releases a claim when a pod goes away. ``release_stale_claims``
selects purely on ``claim_expires < now``, and the TTL is
``DEFAULT_CLAIM_TTL_SECONDS = 900``. There is no SIGTERM drain and no startup
adoption, so an ordinary rollout costs a full 15 minutes of dark cards. Measured
on the same day: five cards claimed at 03:59:04, pod deleted at 04:00:11,
reclaimed at 04:14:24 — exactly one TTL after the last heartbeat.

The two fixes
-------------
1. **Adjudicate only your own claims.** ``claim_is_self`` compares the whole
   token, not the host half, so a previous life's PIDs are never probed. A
   colliding PID in a recycled namespace can no longer be mistaken for a live
   worker, and a dead one can no longer be reported as a crash.

2. **Release dead foreign claims immediately, and charge the ones that are
   faults.** ``release_dead_foreign_claims`` sweeps ``running`` cards whose
   claim belongs to someone else *and* whose recorded worker PID is not alive,
   and puts them back in ``ready`` for the dispatcher to pick up. This is what
   removes the 900-second dark window after a rollout or a crash.
   ``charge_reclaimed_cards`` then spends one unit of retry budget on each
   release the sweep classified as a fault — see "Which reclaims are charged"
   below, which is the whole of part 2's subtlety.

   The liveness test is what makes the sweep safe in every caller, not just the
   gateway. A CLI invocation has its own ``_claimer_id()`` and would otherwise
   consider the live gateway's claims foreign — but the gateway's workers have
   live PIDs, so they are left strictly alone. Where a PID does collide with an
   unrelated live process the card simply falls back to the existing TTL, which
   is the behaviour we already have today.

   The sweep honours the same launch-window grace period the per-row loop below
   it applies, for the same reason and from the same source
   (``_resolve_crash_grace_seconds``, injected). Running ahead of that check
   would give foreign claims a stricter liveness test than the process's own —
   a divergence with no justification behind it, since a card claimed seconds
   ago by any process is a card whose worker may not be on ``/proc`` yet.

Why a release is charged at all, and why it goes back to ``ready``
------------------------------------------------------------------
The first version of this module released the card for free — ``todo``, no
failure recorded — on the reasoning that an infrastructure event is not the
card's fault and must not spend its retry budget. That reasoning conflated two
different questions. Whose claim it was says nothing about whether the *run*
failed, and the sweep's own precondition is that the worker process is provably
dead with no result written. That is a failed run by the same definition
``detect_crashed_workers`` uses one loop later for a worker of our own.

Left uncharged it is also an unbounded loop, which is the concrete failure this
paragraph exists for. A card whose work kills the dispatcher — an OOM, the
2026-08-07 SIGBUS — is reclaimed by the replacement process, dispatched again,
kills it again. Nothing counts, so nothing ever stops it: replayed against a
real board through the shipped engine, six cycles of claim → reclaim → promote
left ``consecutive_failures`` at 0 every single time. The card burns a worker
slot and takes the gateway down with it for as long as the pod keeps coming
back.

``todo`` was half of why the breaker could not see it. ``recompute_ready``
applies its failure-limit guard (#35072) only to ``blocked`` rows; a ``todo``
row is promoted unconditionally, so even a card carrying an exhausted counter
walks straight back to ``ready``. ``ready`` is also what upstream's own crash
path releases to, and what ``_record_task_failure(release_claim=False)``
requires — its trip branch is gated on ``status IN ('ready', 'running')``.

Charging goes through ``_record_task_failure`` with no ``failure_limit``
argument, so the threshold is the one the dispatcher already resolves for every
other failure: per-task ``max_retries``, else ``kanban.failure_limit``, else
``DEFAULT_FAILURE_LIMIT``. No second budget, no new column. A poison card lands
in ``blocked`` with a ``gave_up`` event after the second reclaim and waits for a
human, exactly as a card whose worker crashes twice does.

Which reclaims are charged: the discriminator
---------------------------------------------
Charging *every* reclaim was too blunt, and the arithmetic says why.
``DEFAULT_FAILURE_LIMIT`` is 2 and no profile overrides ``kanban.failure_limit``,
so two reclaims across a card's whole life park it in ``blocked`` behind a
``hermes kanban unblock``. A long-running card that happens to be in flight
during two ordinary deploys is not a failing card, and there is nothing for the
human the trip summons to do about it.

So the sweep classifies each release before handing it to the charge loop, and
the classification turns on **whether the owner belonged to an era this process
could have observed**. Two facts decide it, and both must hold for a release to
be forgiven:

1. *The owner was in a different pod.* Kubernetes replaces a pod for reasons
   that have nothing to do with any card: a rollout, an eviction, a node drain,
   a preemption. The gateway is a ``Deployment``, so a replacement pod gets a
   new name (ReplicaSet hash plus a random suffix) and the host half of the
   claim token changes with it. When the host half is *unchanged*, the pod is
   still the pod this process is running in and the previous process died in
   place — a container restart, which is what a SIGBUS, an OOM kill or a
   liveness-probe kill produces. That is precisely the poison-card signature,
   and it stays charged. **This is the condition that keeps the unbounded loop
   above closed**, so it is not optional.

2. *The claim predates this process.* If the claim was made before this process
   existed, the two never ran concurrently and the owner's disappearance is part
   of the same event that produced this process. If it was made afterwards, the
   owner was alive alongside us and went away on its own — a ``hermes kanban``
   CLI in another pod whose worker died, or a second replica losing a worker.
   That is an observed fault and it is charged. Without this condition a CLI
   invocation from anywhere outside this pod would get free retries forever.

A caveat worth stating rather than discovering: during a rolling update the
outgoing pod and the incoming one overlap, so a card the outgoing pod claims
*after* the new gateway starts fails test 2 and is charged. The window is the
few seconds between the new pod becoming ready and the old one finishing its
SIGTERM drain, and erring toward charging inside it is the safe direction —
under-charging is what reopens the loop.

Two more inputs decide nothing on their own but need saying. A release whose
claim time cannot be read (``current_run_id`` NULL, or the run row gone) is
charged: the discriminator's job is to forgive a *provable* replacement, and an
unknown is not one. And ``socket.gethostname()`` is only the pod name because
the pod does not set ``hostNetwork``; with host networking every pod on a node
would share the node's hostname, test 1 would read every replacement as an
in-place restart, and the behaviour would degrade to charging everything —
which is the pre-discriminator behaviour, not a new failure mode.

Why "this process's start", and where it is read from
-----------------------------------------------------
Test 2 needs a wall-clock instant for "when this process began", comparable to
the epoch seconds ``claim_task`` writes into ``task_runs.started_at``. Three
candidates were considered:

* **Container start** (``/proc/1/stat``, or a file the entrypoint touches).
  Rejected: it is the wrong boundary. A gateway process restarted inside a live
  container by a supervisor would treat its own predecessor's claims as
  belonging to its era and charge them, which they are — but the boundary would
  say the opposite for anything claimed after the container came up. The
  question the discriminator asks is about *processes*, so the answer has to be
  about processes.
* **A timestamp captured at module import.** Nearly right for the gateway, where
  ``kanban_db`` is imported during startup, and it is what this module falls
  back to. Not right in general: it is later than the true process start by
  however long the import graph takes, and it is inherited verbatim across an
  ``os.fork()``, so a forked child would claim its parent's age.
* **``/proc/self/stat`` field 22 against ``/proc/uptime``.** Chosen.
  ``starttime`` is the process's own start expressed in clock ticks since system
  boot; ``/proc/uptime`` is seconds since that same boot. Subtracting the uptime
  from ``time.time()`` gives the boot instant on the wall clock and adding
  ``starttime`` gives the process's. Both readings are taken against the same
  origin, which is what makes the arithmetic correct in a container: Kubernetes
  gives the container its own PID namespace but not its own boot clock, so both
  numbers still refer to the host's boot and the difference between them is
  exact regardless. Reading it fresh (per process, cached by PID) is also
  fork-safe by construction, which the import-time constant is not.

``time.time()`` is a wall clock and can be stepped by NTP. A backward step large
enough to matter would have to exceed the gap between a claim and the next
sweep, and the failure it produces is a charged reclaim that should have been
forgiven — the same safe direction as the rollout overlap above. Using a
monotonic clock instead is not an option: the value has to be comparable with
timestamps another process wrote to the database.

Why the PID stays in the fingerprint after all
----------------------------------------------
This patch used to make a further edit: it replaced upstream's
``re.sub(r'\\bpid \\d+\\b', 'pid N', ...)`` in ``_error_fingerprint`` with a bare
``error_text[:80]``, so that ``pid 1044 not alive`` and ``pid 1045 not alive``
would count as two failures rather than one systemic fault. That edit has been
reverted, because ``_error_fingerprint`` is reachable from exactly two call
sites — both inside ``detect_crashed_workers``, both over the ``crash_details``
error texts — and every message that path can produce is PID-prefixed::

    f"pid {pid} exited with code {code}"
    f"pid {pid} killed by signal {code}"
    f"pid {pid} not alive"

With the PID left in, no two concurrent workers can ever share a fingerprint, so
``_fp_counts`` never reaches the ``>= 3`` the systemic heuristic tests and
``is_systemic`` is dead code. Driving the shipped engine with four of its own
workers all OOM-killed in one tick (``exited with code 137``, distinct PIDs)
produced four fingerprints and an empty ``auto_blocked``: the detector that is
supposed to stop a board where everything is failing the same way could not
fire at all.

The edit was aimed at a cause that fix 1 had already removed. The six
fingerprints of 2026-08-07 existed only because the successor adjudicated its
predecessor's workers; with ``claim_is_self`` in place those cards never enter
``crash_details``, they go through the sweep instead. What is left in that
bucket after the fence is a burst of *our own* workers dying the same way in a
single tick, which is what the heuristic was built for. Reclaims are charged in
their own loop and never join the fingerprint pass, so a rollout that hands back
six cards at once still cannot collapse into one systemic verdict — the property
this module was written to protect, and the reason
``charge_reclaimed_cards`` is a separate pass rather than extra
``crash_details`` entries.
"""

from __future__ import annotations

import json
import os
import time
from typing import NamedTuple, Optional

# ---------------------------------------------------------------------------
# Part 1: inverted fan-out dependency edges
# ---------------------------------------------------------------------------

# Statuses that satisfy the parent gate in ``claim_task`` / ``recompute_ready``.
SETTLED = "('done', 'archived')"

DEPENDENCY_EVENT_KIND = "dependency_repaired"


def _reaches(conn, start: str, target: str) -> bool:
    """True if ``target`` is reachable walking parent->child edges from ``start``."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


def find_deadlocked_children(conn, task_id: str) -> list[str]:
    """Unsettled cards ``task_id`` fanned out after it started and alone gates.

    Two conditions beyond "is an unsettled child", both explained at length in
    the module docstring. The run window — the child's ``created`` event is
    newer than this card's first ``claimed`` event — is what tells a card this
    one fanned out apart from a pipeline successor somebody else planned. The
    ``NOT EXISTS`` is what stops the repair rewriting an edge whose inversion
    would free nothing. Either subquery returning NULL makes the comparison NULL
    and drops the row, which is the conservative answer for a card that reached
    ``running`` without ever being claimed.
    """
    rows = conn.execute(
        "SELECT l.child_id FROM task_links l "
        "JOIN tasks c ON c.id = l.child_id "
        f"WHERE l.parent_id = ? AND c.status NOT IN {SETTLED} "
        "  AND (SELECT MIN(birth.id) FROM task_events birth "
        "        WHERE birth.task_id = l.child_id AND birth.kind = 'created') "
        "      > (SELECT MIN(started.id) FROM task_events started "
        "          WHERE started.task_id = l.parent_id "
        "            AND started.kind = 'claimed') "
        "  AND NOT EXISTS ("
        "        SELECT 1 FROM task_links o "
        "        JOIN tasks op ON op.id = o.parent_id "
        "        WHERE o.child_id = l.child_id "
        "          AND o.parent_id <> l.parent_id "
        f"          AND op.status NOT IN {SETTLED}"
        "  ) "
        "ORDER BY l.child_id",
        (task_id,),
    ).fetchall()
    return [r["child_id"] for r in rows]


def repair_inverted_dependencies(conn, task_id: str, reason: str = "") -> list[str]:
    """Invert every edge that would deadlock a dependency-block on ``task_id``.

    Returns the ids whose edge was inverted. Safe to call when there is nothing
    to repair — the common case — in which case it returns ``[]`` and writes
    nothing. Must be called inside an open write transaction; it does not open
    one of its own.

    Calling it twice is a no-op the second time: the first pass turned every
    edge it touched around, so the card has no outgoing edges left for
    ``find_deadlocked_children`` to find.
    """
    children = find_deadlocked_children(conn, task_id)
    if not children:
        return []

    repaired: list[str] = []
    skipped: list[str] = []
    for child in children:
        # Drop the gating edge first so the cycle probe sees the graph as it
        # will be, not as it was.
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (task_id, child),
        )
        # The probe has to follow the edge about to be *inserted*, not the one
        # just deleted: adding child -> task_id closes a loop exactly when
        # task_id can still reach child by some other path.
        if _reaches(conn, task_id, child):
            # Inverting would close a loop. Put it back and leave this one be.
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (task_id, child),
            )
            skipped.append(child)
            continue
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (child, task_id),
        )
        repaired.append(child)

    if repaired or skipped:
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                task_id,
                None,
                DEPENDENCY_EVENT_KIND,
                json.dumps(
                    {
                        "inverted": repaired,
                        "skipped_would_cycle": skipped,
                        "reason": (reason or "")[:200],
                        "detail": (
                            "cards listed this one as a parent while it waited on "
                            "them; edges inverted so they can be dispatched and "
                            "this card resumes once they are done"
                        ),
                    }
                ),
                int(time.time()),
            ),
        )
    return repaired


# ---------------------------------------------------------------------------
# Part 2: claims fenced to a process life
# ---------------------------------------------------------------------------

RECLAIM_ERROR = "released: claim held by a process that is gone"

RECLAIM_EVENT_KIND = "reclaimed"

# How the sweep read the owner's disappearance. Only ``POD_REPLACED`` is
# forgiven; the module docstring's "Which reclaims are charged" section is the
# argument for each.
POD_REPLACED = "pod_replaced"
PROCESS_DIED_IN_PLACE = "process_died_in_place"
CONCURRENT_OWNER = "concurrent_owner"
CLAIM_TIME_UNKNOWN = "claim_time_unknown"


class Reclaimed(NamedTuple):
    """One sweep's releases, split by whether the card owes a retry for them.

    Both lists are already back at ``ready`` with their runs closed when the
    sweep returns; the split only decides what ``charge_reclaimed_cards`` is
    handed. Keeping ``forgiven`` rather than dropping it is deliberate — the
    caller has no other way to report or log a release it is not charging for,
    and a silent free reclaim is indistinguishable from a sweep that did
    nothing.
    """

    chargeable: list[str]
    forgiven: list[str]


def claim_is_self(lock: Optional[str], claimer_id: str) -> bool:
    """True when ``lock`` was written by this exact process life.

    Replaces upstream's host-prefix comparison, which cannot tell a container
    restart from the process that is currently running.
    """
    return bool(lock) and lock == claimer_id


def claim_host(token: Optional[str]) -> str:
    """The host half of a ``host:pid`` claim token — the pod name in-cluster.

    Split the same way ``detect_crashed_workers`` splits it (``split(':', 1)``),
    so a hostname that somehow contains a colon is read identically here and
    upstream.
    """
    return (token or "").split(":", 1)[0]


# Fallbacks for ``process_start_time`` when ``/proc`` is not there — a
# developer's macOS box, or the unit tests. Captured at import because that is
# the earliest instant this module can observe; see the docstring for why it is
# a fallback and not the primary.
_IMPORT_PID = os.getpid()
_IMPORTED_AT = time.time()

try:
    _CLOCK_TICKS = float(os.sysconf("SC_CLK_TCK")) or 100.0
except (AttributeError, ValueError, OSError):  # pragma: no cover - non-POSIX
    _CLOCK_TICKS = 100.0

# (pid, wall-clock start). Keyed by PID so a fork recomputes instead of
# inheriting its parent's answer.
_PROCESS_START: Optional[tuple[int, float]] = None


def read_proc_start_time() -> Optional[float]:
    """This process's start as wall-clock epoch seconds, from ``/proc``.

    ``None`` when ``/proc`` is absent or unreadable, which is the whole of the
    non-Linux case. Field 22 of ``/proc/self/stat`` is ``starttime`` in clock
    ticks since system boot; ``/proc/uptime`` is seconds since the same boot, so
    ``now - uptime`` is the boot instant and adding ``starttime`` lands on this
    process. Field 2 (``comm``) is parenthesised and may itself contain spaces
    and parentheses, so the only safe place to start splitting is the LAST
    ``)`` — after which ``starttime`` is index 19, fields 1 and 2 having been
    consumed.
    """
    try:
        with open("/proc/self/stat", "rb") as fh:
            stat = fh.read().decode("utf-8", "replace")
        ticks = float(stat[stat.rindex(")") + 1:].split()[19])
        with open("/proc/uptime", "rb") as fh:
            uptime = float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    return time.time() - uptime + ticks / _CLOCK_TICKS


def process_start_time() -> float:
    """Wall-clock epoch seconds at which the calling process began.

    Comparable with the ``int(time.time())`` values ``claim_task`` writes into
    ``task_runs.started_at``, which is the only reason it has to be a wall clock
    rather than a monotonic one. Cached per PID: the value is constant for a
    process, and the fallback in particular must not drift between sweeps or the
    discriminator's boundary would move every tick.
    """
    global _PROCESS_START
    pid = os.getpid()
    if _PROCESS_START is not None and _PROCESS_START[0] == pid:
        return _PROCESS_START[1]
    value = read_proc_start_time()
    if value is None:
        # No ``/proc``. In the process that imported this module, import time is
        # the closest observation available; in a fork of it, the child is new,
        # so now is closer than the parent's import.
        value = _IMPORTED_AT if pid == _IMPORT_PID else time.time()
    _PROCESS_START = (pid, value)
    return value


def classify_reclaim(
    lock: Optional[str],
    claimer_id: str,
    claimed_at: Optional[float],
    process_start: float,
) -> str:
    """Why a dead foreign owner disappeared, as far as the board can tell.

    Returns one of the four module constants. ``POD_REPLACED`` — and only that —
    means the release is infrastructure rather than a fault, so the card keeps
    its retry budget. The reasoning for each arm is in the module docstring
    under "Which reclaims are charged"; the short form is that a claim from this
    same pod means the process died in place (crash, OOM, probe kill) and a
    claim made after this process started means the owner was alive alongside
    us, and both of those are faults this card has to pay for.

    ``process_start`` is truncated before the comparison because ``claim_task``
    writes ``int(time.time())``: a claim made 0.4s after this process began
    records as a whole second *below* the fractional start and would otherwise
    read as pre-boot. Truncating puts that whole second on the charged side,
    which is the same direction the rollout-overlap caveat in the module
    docstring errs in — under-charging is what reopens the loop.
    """
    if claim_host(lock) == claim_host(claimer_id):
        return PROCESS_DIED_IN_PLACE
    try:
        claimed = float(claimed_at)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return CLAIM_TIME_UNKNOWN
    if claimed >= int(process_start):
        return CONCURRENT_OWNER
    return POD_REPLACED


def release_dead_foreign_claims(
    conn,
    claimer_id: str,
    pid_alive,
    grace_seconds=None,
    process_start: Optional[float] = None,
) -> Reclaimed:
    """Return ``running`` cards whose owner is gone to ``ready``.

    A card qualifies when all four hold:

    * its ``claim_lock`` is not this process life's token,
    * it has a recorded ``worker_pid``,
    * it is past the launch-window grace period,
    * that PID is not alive.

    ``grace_seconds`` is ``_resolve_crash_grace_seconds`` — injected rather than
    imported so this module never depends on ``kanban_db``. Omitting it skips
    the grace check, which is what the tests want and what a caller with no
    ``started_at`` column can live with. ``process_start`` defaults to this
    process's own start and exists as an argument for the same reason: so a test
    can name the boundary instead of racing it.

    The claim instant comes from ``task_runs.started_at`` of the row
    ``tasks.current_run_id`` points at, which ``claim_task`` writes in the same
    transaction as the claim. ``tasks.started_at`` is NOT that value — it is
    ``COALESCE(started_at, ?)``, the first start the card ever had, so a
    re-claimed card would be judged on a run that ended long ago.

    Must be called inside an open write transaction. The ``chargeable`` half of
    the result is what the caller owes to ``charge_reclaimed_cards`` once that
    transaction has closed.

    **A PID collision is more likely than it used to be**, for the reason in the
    module docstring: the pod no longer shares a process namespace, so a restart
    hands out PIDs from 1 and a stale claim recording PID 4 will often name a
    process that is very much alive. ``pid_alive(pid)`` then holds the card
    rather than reclaiming it, and the TTL is what eventually frees it. That is
    the fail-safe direction — a card comes back late instead of being taken from
    something still working on it — but "late" is the TTL, not seconds.
    """
    if process_start is None:
        process_start = process_start_time()

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.claim_lock, t.current_run_id, "
        "       t.started_at, r.started_at AS claimed_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.claim_lock IS NOT NULL "
        "AND t.worker_pid IS NOT NULL"
    ).fetchall()

    chargeable: list[str] = []
    forgiven: list[str] = []
    now = int(time.time())
    grace = None
    for row in rows:
        lock = row["claim_lock"] or ""
        if claim_is_self(lock, claimer_id):
            continue
        try:
            pid = int(row["worker_pid"])
        except (TypeError, ValueError):
            continue
        if grace_seconds is not None:
            started_at = row["started_at"]
            if started_at is not None:
                if grace is None:
                    grace = grace_seconds()
                if now - started_at < grace:
                    # Too new to adjudicate: the worker may not be on /proc yet.
                    # Same test, same source as the per-row loop in
                    # ``detect_crashed_workers``.
                    continue
        if pid_alive(pid):
            # Either a real live worker or a PID collision. The TTL remains the
            # backstop; never take a card away from something that might be
            # running it.
            continue

        fate = classify_reclaim(lock, claimer_id, row["claimed_at"], process_start)
        charged = fate != POD_REPLACED

        # ``ready``, not ``todo``: ``recompute_ready`` promotes a ``todo`` row
        # without consulting its failure counter, which is how a card that kills
        # the dispatcher used to escape the breaker forever. It is also the
        # status ``_record_task_failure`` needs to see to trip.
        conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL, current_run_id = NULL "
            "WHERE id = ?",
            (row["id"],),
        )
        # ``ended_at IS NULL`` mirrors ``_end_run``: a run already closed by some
        # other path must not have its outcome rewritten to ``reclaimed``.
        conn.execute(
            "UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed', "
            "ended_at = ?, error = ? WHERE task_id = ? AND status = 'running' "
            "AND ended_at IS NULL",
            (now, RECLAIM_ERROR, row["id"]),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                row["id"],
                row["current_run_id"],
                RECLAIM_EVENT_KIND,
                json.dumps(
                    {
                        "stale_lock": lock,
                        "worker_pid": pid,
                        "claimer": claimer_id,
                        "reason": "owner_process_gone",
                        "fenced": True,
                        # The discriminator's verdict, on the board rather than
                        # only in a log line: a card that is not charged has to
                        # say so, or the counter and the history disagree and
                        # nobody can tell which is lying.
                        "owner_fate": fate,
                        "charged": charged,
                    }
                ),
                now,
            ),
        )
        (chargeable if charged else forgiven).append(row["id"])
    return Reclaimed(chargeable, forgiven)


def charge_reclaimed_cards(conn, task_ids, record_failure) -> list[str]:
    """Spend one retry on each card the sweep handed back. Returns those parked.

    ``record_failure`` is ``_record_task_failure``, injected for the same reason
    ``pid_alive`` is. It is called the way the crash path calls it — the card is
    already back at ``ready`` with its run closed, so no claim to release and no
    run to end — and with no ``failure_limit``, so the threshold is the one the
    dispatcher resolves for every other failure kind rather than a second budget
    invented here.

    Must be called OUTSIDE the transaction the sweep ran in:
    ``_record_task_failure`` opens its own ``write_txn`` and ``write_txn`` does
    not nest.

    Deliberately a separate loop from the ``crash_details`` pass rather than an
    extra entry in it. Every card released by one event carries the same
    ``RECLAIM_ERROR`` text, so folding them into ``_fp_counts`` would make any
    three of them look systemic, drop ``failure_limit`` to 1 and abandon the lot
    on their first reclaim — the 2026-08-07 outcome, reached by a new route.
    The discriminator does not change that: it decides *which* ids arrive here,
    and a same-pod container restart still delivers every in-flight card at once
    with one identical error text.
    """
    tripped: list[str] = []
    for task_id in task_ids:
        if record_failure(
            conn,
            task_id,
            error=RECLAIM_ERROR,
            outcome=RECLAIM_EVENT_KIND,
            release_claim=False,
            end_run=False,
            event_payload_extra={"reason": "owner_process_gone", "fenced": True},
        ):
            tripped.append(task_id)
    return tripped
