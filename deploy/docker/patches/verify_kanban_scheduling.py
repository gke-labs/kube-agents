#!/usr/bin/env python3
"""Build gate for the kanban scheduling patches.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_scheduling.py``. The applier only proves six anchors matched
exactly once. That says nothing about whether the engine those six edits produce
actually schedules correctly, and all three faults they fix were emergent
scheduling behaviour rather than a bad string — so every case below is driven
against the real patched ``hermes_cli.kanban_db`` on a real board, through the
real ``create_task`` / ``claim_task`` / ``block_task`` / ``detect_crashed_workers``
/ ``_record_task_failure`` / ``recompute_ready``, in the order ``dispatch_once``
calls them.

  A. Dependency deadlock. A card creates work with ``parents=[<itself>]`` and
     then blocks on it. Before the patch the children were permanently
     unclaimable and the parent respawned every few seconds; after it, the
     children dispatch and the parent waits on them for real. The three
     negative cases are the ones the first version of the patch got wrong.
  B. Claim fencing. A gateway process goes away. Its successor must not
     adjudicate the dead process's worker PIDs, must hand those cards straight
     back instead of leaving them dark for the 900-second claim TTL, and must
     charge exactly the releases that are faults — an in-place death, yes; a
     pod Kubernetes replaced, no.
  C. Failure fingerprints. The systemic-crash heuristic the fence stops
     misfiring must still fire when it should.
  D. Breaker counter. Nine scenarios, the same nine that were executed against
     the live image before the breaker edits were written:

       1. protocol ``force_trip`` (limit 3)          FIXED    -- was reverted in-tick
       2. systemic ``failure_limit=1``               FIXED    -- was reverted in-tick
       3. an ordinary two-failure trip               UNCHANGED - no inflation
       4. ``assign_task`` recovery                   PRESERVED - the rejected fix broke this
       4b. ``assign_task`` to the SAME profile       CHANGED   -- no longer recovers
       5. ``unblock_task`` recovery                  PRESERVED
       6. parent-gated card, never tripped           PRESERVED
       7. ``max_retries=5`` override                 FIXED    -- honours the override
       8. worker ``kanban_block`` stickiness         UNCHANGED
       9. spawn path (``release_claim``) at limit 1  FIXED    -- the second patched bind

     4, 5, 6 and 8 are preservation tests. They are not decoration: they are
     exactly the cases the rejected event-reader fix broke, and they are the
     reason that fix keeps ``consecutive_failures`` as the single arbiter. That
     rejected version passed every anchor check and silently pinned reassigned
     cards in ``blocked`` forever. 4b is the one case the patch deliberately
     DOES change, pinned here so it stays a decision rather than a surprise.

Sections B and D overlap on purpose: the fence decides which cards reach the
breaker and the charge loop is a caller of the very function D exercises, so a
regression in either is only visible when both run against the same engine.

Usage::

    cd /opt/hermes && python3 verify_kanban_scheduling.py
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

FAILURES: list[str] = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


from hermes_cli import kanban_db as K  # noqa: E402

# Imported under its own name as well as through the trailer, so this gate fails
# if the module did not land at ``hermes_cli/kanban_scheduling.py`` — and so the
# discriminator's constants can be asserted by name rather than by string.
from hermes_cli import kanban_scheduling as KS  # noqa: E402

TMP = Path(tempfile.mkdtemp())
_BOARDS = itertools.count()


def fresh():
    """A real board, created through the engine's own schema path.

    The name comes from a counter. Deriving it from ``len(list(TMP.iterdir()))``
    looks equivalent and is not, and the way it fails is silent: ``K.connect``
    opens the board in WAL mode, so each one owns a ``-wal`` and a ``-shm`` as
    well as the ``.db`` and an ``.init.lock``. Rebinding ``conn = fresh()``
    drops the last reference to the previous connection, CPython closes it, and
    SQLite *deletes* that board's ``-wal`` and ``-shm``. Files therefore leave
    the directory as well as arrive; the count is not monotonic, it reaches a
    fixed point, and from there every "fresh" board reopens one file that
    already holds every card the sections before it left running.

    That is invisible to a check which only asserts on its own card -- which is
    most of them -- and fatal to one that asserts on the whole of a sweep's
    result. It cost a Cloud Build failure that read exactly like a defect in
    ``release_dead_foreign_claims``: a third card, left ``running`` by an
    earlier section, was swept alongside the two this board had put there.

    The emptiness assertion is not paranoia, it is the gate that turns the next
    recurrence into one obvious line instead of a day of reading the sweep. It
    raises rather than calling ``check`` because a board that is not fresh
    invalidates every check after it, so there is nothing to be gained by
    carrying on and counting failures.
    """
    conn = K.connect(TMP / f"kanban{next(_BOARDS)}.db")
    leftovers = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    if leftovers:
        raise SystemExit(
            "verify_kanban_scheduling: fixture defect — a 'fresh' board opened "
            f"with {leftovers} card(s) already on it. Every check from here on "
            "would be adjudicating another section's leftovers."
        )
    return conn


def status(conn, tid):
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
    return row["status"] if row else None


def failures(conn, tid):
    row = conn.execute(
        "SELECT consecutive_failures FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    return int(row["consecutive_failures"] or 0) if row else None


def kinds(conn, tid):
    return [
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
        )
    ]


def last_event(conn, tid, kind):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (tid, kind),
    ).fetchone()
    return json.loads(row["payload"]) if row and row["payload"] else {}


def new_card(conn, title, parents=()):
    task = K.create_task(
        conn, title=title, assignee="platform", parents=tuple(parents)
    )
    return task.id if hasattr(task, "id") else str(task)


# --- A. The self-parenting deadlock -----------------------------------------
print("dependency deadlock repair:")
conn = fresh()

parent = new_card(conn, "Fleet-wide Security Baseline Assessment")
K.recompute_ready(conn)
claimed = K.claim_task(conn, parent)
check("the orchestrator card claims normally", claimed is not None)

kids = [new_card(conn, f"Audit cluster {n}", parents=[parent]) for n in (1, 2, 3)]
K.recompute_ready(conn)
check(
    "children parented to a running card start unclaimable",
    all(K.claim_task(conn, k) is None for k in kids),
    "the invariant this patch exists to work around has changed upstream",
)

blocked = K.block_task(
    conn, parent, reason="waiting on the three cluster audits", kind="dependency"
)
check("the dependency block is accepted", blocked is True)
check("the repair event is recorded", "dependency_repaired" in kinds(conn, parent))

K.recompute_ready(conn)
claimed_kids = [k for k in kids if K.claim_task(conn, k) is not None]
check(
    "every child dispatches after the block",
    len(claimed_kids) == 3,
    f"only {len(claimed_kids)}/3 became claimable",
)
check(
    "the blocking card now waits instead of respawning",
    K.claim_task(conn, parent) is None,
    "the parent is claimable while its prerequisites are unfinished",
)

for k in kids:
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (k,))
conn.commit()
K.recompute_ready(conn)
check(
    "the wait resolves once the children finish",
    K.claim_task(conn, parent) is not None,
    f"parent stuck in {status(conn, parent)!r}",
)

# A real fan-in must survive one of its inputs blocking. The sink was planned
# before a was ever claimed, so it is nobody's fan-out, and releasing a would be
# pointless anyway while b is unfinished. (The first version of this patch
# failed exactly here, inverting a -> sink.)
conn = fresh()
a = new_card(conn, "researcher a")
b = new_card(conn, "researcher b")
sink = new_card(conn, "synthesizer", parents=[a, b])
K.recompute_ready(conn)
K.claim_task(conn, a)
K.block_task(conn, a, reason="waiting on an external approval", kind="dependency")
check(
    "a correct fan-in is left alone",
    "dependency_repaired" not in kinds(conn, a),
    "the repair fired on a graph that was already correct",
)
K.recompute_ready(conn)
check("the fan-in sink still waits for its inputs", K.claim_task(conn, sink) is None)
check(
    "the blocked input is still an input, not a successor",
    conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (a, sink)
    ).fetchone()
    is not None,
    "the pipeline was turned around",
)

# An ordinary pipeline stage that blocks for a reason of its own. This is the
# scenario the patch shipped broken: by the time `mid` runs its prerequisite is
# `done`, so the parent-set test that used to guard the repair could not tell it
# apart from the t_ab112f5b deadlock and inverted mid -> tail, dispatching the
# final stage ahead of the middle one. Driven through claim_task rather than a
# raw UPDATE precisely because that is what makes the parents settled.
conn = fresh()
upstream = new_card(conn, "prerequisite")
mid = new_card(conn, "middle stage", parents=[upstream])
tail = new_card(conn, "final stage", parents=[mid])
conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (upstream,))
conn.commit()
K.recompute_ready(conn)
check(
    "the middle stage claims once its prerequisite is done",
    K.claim_task(conn, mid) is not None,
    f"mid stuck in {status(conn, mid)!r}",
)
K.block_task(conn, mid, reason="waiting on an external approval", kind="dependency")
check(
    "a pipeline stage that blocks is left alone",
    "dependency_repaired" not in kinds(conn, mid),
    "the repair rewrote a pipeline it had no hand in creating",
)
check(
    "the pipeline keeps its direction",
    conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?", (mid, tail)
    ).fetchone()
    is not None,
)
K.recompute_ready(conn)
check(
    "the final stage still waits its turn",
    K.claim_task(conn, tail) is None,
    "the successor was dispatched ahead of the stage it follows",
)

# --- B. Claim fencing across a container restart ----------------------------
print("claim fencing:")
conn = fresh()

DEAD_PID = 4_194_303  # above every plausible pid_max, so _pid_alive is False
check("the probe pid really is dead", not K._pid_alive(DEAD_PID))

pod = socket.gethostname()
predecessor = f"{pod}:4"  # same pod name, the process that died
check(
    "the predecessor's token would have passed upstream's host-prefix test",
    predecessor.startswith(f"{pod.split(':')[0]}:"),
)

orphan = new_card(conn, "card in flight when the gateway crashed")
K.recompute_ready(conn)
K.claim_task(conn, orphan, claimer=predecessor)
conn.execute(
    "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (DEAD_PID, orphan)
)
conn.commit()
check("the card is running under the old claim", status(conn, orphan) == "running")

crashed = K.detect_crashed_workers(conn)
check(
    "the successor does not report its predecessor's worker as a crash",
    orphan not in crashed,
    "a recycled PID namespace was adjudicated as if it were still ours",
)
check(
    "the card is handed back rather than left for the 900s TTL",
    status(conn, orphan) == "ready",
    f"left in {status(conn, orphan)!r}",
)
events = kinds(conn, orphan)
check("the release is recorded as reclaimed", "reclaimed" in events)
check(
    "one gateway crash costs the card one retry, not the card",
    "gave_up" not in events and "crashed" not in events,
    f"events: {events}",
)
check(
    "and that retry is actually spent",
    failures(conn, orphan) == 1,
    f"consecutive_failures is {failures(conn, orphan)}, so nothing bounds a reclaim loop",
)
check(
    "the release says why it was charged",
    last_event(conn, orphan, "reclaimed").get("owner_fate")
    == KS.PROCESS_DIED_IN_PLACE
    and last_event(conn, orphan, "reclaimed").get("charged") is True,
    f"reclaimed payload: {last_event(conn, orphan, 'reclaimed')}",
)


def reclaim_cycle(conn, tid, cycle):
    """One turn of claim -> the dispatcher dies -> the successor sweeps."""
    K.claim_task(conn, tid, claimer=f"{pod}:{900 + cycle}")
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (DEAD_PID, tid)
    )
    conn.commit()
    K.detect_crashed_workers(conn)
    K.recompute_ready(conn)


# A card that kills the dispatcher gets reclaimed by whatever comes up next.
# Uncharged that is a closed loop — claim, restart, reclaim, promote, forever —
# so the breaker has to be on this path, at its own threshold.
conn = fresh()
poison = new_card(conn, "card whose work takes the gateway down with it")
K.recompute_ready(conn)
for cycle in range(K.DEFAULT_FAILURE_LIMIT + 2):
    if status(conn, poison) == "blocked":
        break
    reclaim_cycle(conn, poison, cycle)
check(
    "a card that keeps killing its dispatcher is parked instead of cycling",
    status(conn, poison) == "blocked",
    f"still {status(conn, poison)!r} with {failures(conn, poison)} failures after "
    f"{K.DEFAULT_FAILURE_LIMIT + 2} reclaims",
)
check(
    "the breaker used its own budget rather than a second one",
    failures(conn, poison) >= K.DEFAULT_FAILURE_LIMIT,
    f"parked at {failures(conn, poison)} of {K.DEFAULT_FAILURE_LIMIT}",
)
check("parking the poison card is announced", "gave_up" in kinds(conn, poison))
check(
    "a parked card stays parked",
    K.claim_task(conn, poison) is None,
    "recompute_ready promoted a card the breaker had given up on",
)

# The founding property of this patch: one infrastructure event must never
# abandon every card that happened to be in flight. Six identical reclaim
# messages must not read as one systemic fault. The claims here carry this pod's
# own name, so they are the charged arm — which is the arm where the property
# has to hold, since a forgiven release never reaches _record_task_failure at
# all.
conn = fresh()
fleet = [new_card(conn, f"card {n} in flight when the container died") for n in range(6)]
K.recompute_ready(conn)
for n, tid in enumerate(fleet):
    K.claim_task(conn, tid, claimer=f"{pod}:{800 + n}")
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (DEAD_PID, tid)
    )
conn.commit()
K.detect_crashed_workers(conn)
check(
    "a container restart hands every in-flight card back at once",
    all(status(conn, t) == "ready" for t in fleet),
    f"statuses: {[status(conn, t) for t in fleet]}",
)
check(
    "and abandons none of them",
    all(failures(conn, t) == 1 for t in fleet),
    f"failures: {[failures(conn, t) for t in fleet]}",
)

# Our own dead worker must still be adjudicated — the fence narrows the check,
# it must not disable it.
conn = fresh()
mine = new_card(conn, "card whose worker really did die")
K.recompute_ready(conn)
K.claim_task(conn, mine)
conn.execute(
    "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (DEAD_PID, mine)
)
conn.commit()
check(
    "our own dead worker is still detected",
    mine in K.detect_crashed_workers(conn),
    "the fence disabled crash detection instead of narrowing it",
)

# A live foreign worker is never taken away.
conn = fresh()

live = new_card(conn, "card held by a live process elsewhere")
K.recompute_ready(conn)
K.claim_task(conn, live, claimer="some-other-pod:7")
conn.execute(
    "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (os.getpid(), live)
)
conn.commit()
K.detect_crashed_workers(conn)
check(
    "a live foreign worker keeps its claim",
    status(conn, live) == "running",
    "the sweep reclaimed a card from a process that is still running it",
)

# --- B2. The discriminator: a rollout is not a retry ------------------------
#
# Everything above charges, because everything above happens inside this pod.
# The boundary the discriminator draws is between that and a pod Kubernetes
# took away, and it is drawn with two facts: a different pod name, and a claim
# older than this process. Both branches are driven through the engine below,
# because the whole point is what ``detect_crashed_workers`` does with them.
print("reclaim discriminator:")

# Inside the image this is Linux and the /proc reader is the one that must
# answer; on a developer's machine it is absent and the import-time fallback
# must. Asserting the equivalence rather than the Linux answer keeps the script
# runnable on both without going vacuous on either.
HAVE_PROC = Path("/proc/self/stat").exists()
check(
    "the /proc reader answers exactly when /proc is there",
    (KS.read_proc_start_time() is not None) == HAVE_PROC,
    "on Linux the fallback is in use, so the boundary is import time rather "
    "than process start — see kanban_scheduling.process_start_time"
    if HAVE_PROC
    else "the reader returned a value with no /proc to read",
)
PROCESS_START = KS.process_start_time()
check(
    "and it is a plausible wall-clock instant",
    0 <= time.time() - PROCESS_START < 3600,
    f"process_start_time() is {PROCESS_START}, now is {time.time()}",
)
check(
    "the boundary does not move between sweeps",
    KS.process_start_time() == PROCESS_START,
    "a boundary that drifts reclassifies the same claim differently each tick",
)

PRE_BOOT = int(PROCESS_START) - 600
OTHER_POD = "platform-agent-gateway-75b5f6ddf6-7dkd7"


def backdate_claim(conn, tid, when):
    """Move the *current run's* start, which is what the sweep reads.

    ``tasks.started_at`` is left alone deliberately: it is
    ``COALESCE(started_at, ?)``, the first start the card ever had, and the
    sweep uses it only for the launch-window grace check.
    """
    conn.execute(
        "UPDATE task_runs SET started_at = ? WHERE id = "
        "(SELECT current_run_id FROM tasks WHERE id = ?)",
        (int(when), tid),
    )


def dead_foreign_claim(conn, tid, claimer, claimed_at=None, drop_run=False):
    """Put ``tid`` into the state a departed owner leaves behind."""
    K.claim_task(conn, tid, claimer=claimer)
    if claimed_at is not None:
        backdate_claim(conn, tid, claimed_at)
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?",
        (DEAD_PID, tid),
    )
    if drop_run:
        conn.execute("UPDATE tasks SET current_run_id = NULL WHERE id = ?", (tid,))
    conn.commit()


conn = fresh()
rolled = new_card(conn, "card in flight when the pod was replaced")
K.recompute_ready(conn)
dead_foreign_claim(conn, rolled, f"{OTHER_POD}:4", claimed_at=PRE_BOOT)
K.detect_crashed_workers(conn)
check(
    "a pre-boot claim from a replaced pod is handed straight back",
    status(conn, rolled) == "ready",
    f"left in {status(conn, rolled)!r}",
)
check(
    "and costs the card nothing",
    failures(conn, rolled) == 0,
    f"consecutive_failures is {failures(conn, rolled)} — a rollout charged a retry",
)
check(
    "and says on the board that it was forgiven",
    last_event(conn, rolled, "reclaimed").get("owner_fate") == KS.POD_REPLACED
    and last_event(conn, rolled, "reclaimed").get("charged") is False,
    f"reclaimed payload: {last_event(conn, rolled, 'reclaimed')}",
)

# The same foreign pod, but the claim was made while this process was already
# running: the owner was alive alongside us and went away on its own. A fault,
# and charged, or a CLI in another pod would hand out free retries forever.
conn = fresh()
concurrent = new_card(conn, "card claimed by another pod after we started")
K.recompute_ready(conn)
dead_foreign_claim(conn, concurrent, f"{OTHER_POD}:5", claimed_at=int(time.time()))
K.detect_crashed_workers(conn)
check(
    "a claim made after this process started is still handed back",
    status(conn, concurrent) == "ready",
    f"left in {status(conn, concurrent)!r}",
)
check(
    "but it is charged, because we watched that owner die",
    failures(conn, concurrent) == 1
    and last_event(conn, concurrent, "reclaimed").get("owner_fate")
    == KS.CONCURRENT_OWNER,
    f"failures {failures(conn, concurrent)}, payload "
    f"{last_event(conn, concurrent, 'reclaimed')}",
)

# No run row to read the claim instant from. An unknown is not a provable
# replacement, so it is charged.
conn = fresh()
runless = new_card(conn, "card whose run row is gone")
K.recompute_ready(conn)
dead_foreign_claim(conn, runless, f"{OTHER_POD}:6", drop_run=True)
K.detect_crashed_workers(conn)
check(
    "an unreadable claim time is charged rather than forgiven",
    failures(conn, runless) == 1
    and last_event(conn, runless, "reclaimed").get("owner_fate")
    == KS.CLAIM_TIME_UNKNOWN,
    f"failures {failures(conn, runless)}, payload "
    f"{last_event(conn, runless, 'reclaimed')}",
)

# The arithmetic this whole section exists for. DEFAULT_FAILURE_LIMIT is 2 and
# nothing overrides it, so before the discriminator the second deploy a
# long-lived card sat through parked it in ``blocked`` behind a
# ``hermes kanban unblock``. Four replacements here, twice the limit.
conn = fresh()
survivor = new_card(conn, "long-running card that sits through several deploys")
K.recompute_ready(conn)
for cycle in range(K.DEFAULT_FAILURE_LIMIT + 2):
    dead_foreign_claim(
        conn,
        survivor,
        f"platform-agent-gateway-{cycle}-{cycle:05d}:{7 + cycle}",
        claimed_at=PRE_BOOT,
    )
    K.detect_crashed_workers(conn)
    K.recompute_ready(conn)
check(
    f"{K.DEFAULT_FAILURE_LIMIT + 2} pod replacements never park the card",
    status(conn, survivor) == "ready" and failures(conn, survivor) == 0,
    f"{status(conn, survivor)!r} with {failures(conn, survivor)} failures — the "
    "discriminator is not forgiving replacements",
)
check(
    "and none of them was recorded as a fault",
    "gave_up" not in kinds(conn, survivor) and "crashed" not in kinds(conn, survivor),
    f"events: {kinds(conn, survivor)}",
)

# The split itself, called directly: the sweep has to report the forgiven
# releases as well as the charged ones, or a free reclaim is indistinguishable
# from a sweep that did nothing.
conn = fresh()
gone_pod = new_card(conn, "held by a pod that was replaced")
died_here = new_card(conn, "held by a process that died in place")
K.recompute_ready(conn)
K.claim_task(conn, gone_pod, claimer=f"{OTHER_POD}:8")
backdate_claim(conn, gone_pod, PRE_BOOT)
K.claim_task(conn, died_here, claimer=f"{pod}:9")
backdate_claim(conn, died_here, PRE_BOOT)
conn.execute("UPDATE tasks SET worker_pid = ?, started_at = 1", (DEAD_PID,))
conn.commit()
with K.write_txn(conn):
    swept = KS.release_dead_foreign_claims(
        conn, K._claimer_id(), K._pid_alive, None, PROCESS_START
    )
check(
    "the sweep reports its releases split by fault",
    isinstance(swept, KS.Reclaimed),
    f"returned {type(swept).__name__}",
)
check(
    "the replaced pod's card is in the forgiven half",
    list(swept.forgiven) == [gone_pod],
    f"forgiven: {list(swept.forgiven)}",
)
check(
    "the in-place death is in the chargeable half",
    list(swept.chargeable) == [died_here],
    f"chargeable: {list(swept.chargeable)}",
)
check(
    "both are back at ready either way",
    status(conn, gone_pod) == "ready" and status(conn, died_here) == "ready",
    f"{status(conn, gone_pod)!r} / {status(conn, died_here)!r}",
)

# --- C. A burst of identical crashes must still look systemic ---------------
#
# ``_error_fingerprint`` is reachable from exactly two call sites, both inside
# ``detect_crashed_workers``, and every message that path builds carries a pid.
# So the ``pid N`` substitution is not cosmetic: without it no two concurrent
# workers can share a bucket, ``_fp_counts`` never reaches 3, and the heuristic
# that halts a board where everything is failing the same way is dead code.
# This patch leaves the substitution alone — the fence above is what keeps a
# predecessor's workers out of the bucket in the first place.
print("failure fingerprints:")
for template in ("pid {} not alive", "pid {} exited with code 137"):
    prints = {K._error_fingerprint(template.format(p)) for p in range(1044, 1050)}
    check(
        f"six workers felled by one event share a fingerprint ({template})",
        len(prints) == 1,
        f"split into {len(prints)} — the systemic heuristic can never reach 3",
    )
check(
    "distinct faults keep distinct fingerprints",
    K._error_fingerprint("pid 1044 exited with code 137")
    != K._error_fingerprint("pid 1044 killed by signal 9"),
)

# And the detector it feeds must actually fire, through the real engine.
DEAD_PIDS = [DEAD_PID - n for n in range(4)]
check("the probe pids really are dead", not any(K._pid_alive(p) for p in DEAD_PIDS))

conn = fresh()
burst = [new_card(conn, f"card {n} whose worker was OOM-killed") for n in range(4)]
K.recompute_ready(conn)
for tid, p in zip(burst, DEAD_PIDS):
    K.claim_task(conn, tid)
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (p, tid)
    )
conn.commit()
K.detect_crashed_workers(conn)
check(
    "four of our own workers dying the same way in one tick reads as systemic",
    all(status(conn, t) == "blocked" for t in burst),
    f"statuses: {[status(conn, t) for t in burst]} — is_systemic never fired",
)

# Below the heuristic's threshold nothing is systemic: two crashes are two
# crashes, and each card keeps the rest of its budget.
conn = fresh()
pair = [new_card(conn, f"card {n} whose worker died alone") for n in range(2)]
K.recompute_ready(conn)
for tid, p in zip(pair, DEAD_PIDS):
    K.claim_task(conn, tid)
    conn.execute(
        "UPDATE tasks SET worker_pid = ?, started_at = 1 WHERE id = ?", (p, tid)
    )
conn.commit()
K.detect_crashed_workers(conn)
check(
    "two crashes in a tick are just two crashes",
    all(status(conn, t) == "ready" for t in pair),
    f"statuses: {[status(conn, t) for t in pair]} — the heuristic fired below 3",
)

# --- D. The breaker counter --------------------------------------------------
#
# Driven on an in-memory board rather than through ``fresh()``: the subject here
# is two SQL binds and a threshold, and ``claim_task`` would drag
# ``get_current_board`` and the ``$HERMES_HOME`` layout into a test that has no
# use for either.

# What ``dispatch_once`` passes to ``recompute_ready`` under the shipped
# configuration (``kanban.failure_limit`` unset on every profile).
DISPATCHER_LIMIT = K.DEFAULT_SPAWN_FAILURE_LIMIT
PROTOCOL_LIMIT = K._PROTOCOL_VIOLATION_FAILURE_LIMIT
# The constant the patch itself floors against. It is a distinct name from
# ``DEFAULT_SPAWN_FAILURE_LIMIT`` above, currently aliased to it in kanban_db.py
# (``DEFAULT_SPAWN_FAILURE_LIMIT = DEFAULT_FAILURE_LIMIT``). Referencing both
# separately means the day they diverge this gate fails rather than passing on a
# coincidence.
FLOOR = K.DEFAULT_FAILURE_LIMIT


def board() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(K.SCHEMA_SQL)
    return conn


def make(conn, tid, *, status="ready", max_retries=None):
    conn.execute(
        "INSERT INTO tasks (id, title, status, created_at, "
        "consecutive_failures, max_retries) VALUES (?, ?, ?, ?, 0, ?)",
        (tid, tid, status, int(time.time()), max_retries),
    )
    conn.commit()


def claim(conn, tid):
    """Put ``tid`` into the state ``claim_task`` leaves behind.

    Open-coded for the reason given above; the columns below are exactly what
    its UPDATE plus ``_start_run`` write.
    """
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO task_runs (task_id, status, claim_lock, worker_pid, "
        "started_at) VALUES (?, 'running', ?, ?, ?)",
        (tid, f"verify-{tid}", 4242, now),
    )
    conn.execute(
        "UPDATE tasks SET status = 'running', claim_lock = ?, "
        "claim_expires = ?, worker_pid = ?, started_at = ?, "
        "current_run_id = ? WHERE id = ?",
        (f"verify-{tid}", now + 900, 4242, now, cur.lastrowid, tid),
    )
    conn.commit()


def state(conn, tid):
    row = conn.execute(
        "SELECT status, consecutive_failures FROM tasks WHERE id = ?", (tid,)
    ).fetchone()
    return row["status"], int(row["consecutive_failures"])


def gave_up_payload(conn, tid):
    return last_event(conn, tid, "gave_up")


def stored_from_payload(payload):
    """Reconstruct ``consecutive_failures`` from the untouched ``gave_up`` row.

    Two arms, because the floor the patch applies is INVISIBLE in the payload:
    it only fires when there is no per-task override, and the payload records
    that as ``limit_source``, not as a number. ``max(failures,
    effective_limit)`` alone is right for the ``task`` arm and wrong for the
    systemic one, which stores 2 while its payload reads
    ``{"failures": 1, "effective_limit": 1}``. The audit trail is the only
    account of why a card is blocked, so a formula that is right for three of
    four arms is not good enough to document.
    """
    stored = max(payload.get("failures", 0), payload.get("effective_limit", 0))
    if payload.get("limit_source") != "task":
        stored = max(stored, FLOOR)
    return stored


print(f"breaker counter (dispatcher limit {DISPATCHER_LIMIT}):")

# --- 1, 2, 7, 9: the arms that used to be reverted in the same tick ---------
conn = board()

# 1. Protocol violation. detect_crashed_workers adjudicates its own streak and
#    passes force_trip=True, so the raw counter lands at 1 -- below the
#    dispatcher's 2, which is what let recompute_ready undo the trip.
make(conn, "proto")
K._record_task_failure(
    conn,
    "proto",
    error="clean exit without a terminal kanban call",
    outcome="crashed",
    failure_limit=PROTOCOL_LIMIT,
    force_trip=True,
)
check(
    "1. protocol force_trip stores the threshold it was decided against",
    state(conn, "proto") == ("blocked", PROTOCOL_LIMIT),
    f"got {state(conn, 'proto')}, want ('blocked', {PROTOCOL_LIMIT})",
)
promoted = K.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "1. protocol trip survives recompute_ready",
    state(conn, "proto")[0] == "blocked" and promoted == 0,
    f"state {state(conn, 'proto')}, promoted {promoted}",
)

# 2. Systemic same-error crash. The caller lowers the limit to 1.
make(conn, "systemic")
K._record_task_failure(
    conn, "systemic", error="container OOMKilled", outcome="crashed",
    failure_limit=1,
)
check(
    "2. systemic trip is floored at the dispatcher default",
    state(conn, "systemic") == ("blocked", DISPATCHER_LIMIT),
    f"got {state(conn, 'systemic')}, want ('blocked', {DISPATCHER_LIMIT})",
)
promoted = K.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "2. systemic trip survives recompute_ready",
    state(conn, "systemic")[0] == "blocked" and promoted == 0,
    f"state {state(conn, 'systemic')}, promoted {promoted}",
)

# 7. A per-task max_retries override must be honoured, not flattened to the
#    module default -- recompute_ready resolves against max_retries first.
make(conn, "override", max_retries=5)
K._record_task_failure(
    conn, "override", error="clean exit without a terminal kanban call",
    outcome="crashed", failure_limit=5, force_trip=True,
)
check(
    "7. max_retries override is stored, not the default floor",
    state(conn, "override") == ("blocked", 5),
    f"got {state(conn, 'override')}, want ('blocked', 5)",
)
promoted = K.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "7. override trip survives recompute_ready",
    state(conn, "override")[0] == "blocked" and promoted == 0,
    f"state {state(conn, 'override')}, promoted {promoted}",
)

# 9. The spawn-failure path takes the OTHER patched UPDATE (release_claim=True,
#    the one whose WHERE clause is ('running', 'ready')). Without its own case
#    that bind could be left unpatched and everything above would still pass.
make(conn, "spawn")
claim(conn, "spawn")
K._record_task_failure(
    conn, "spawn", error="failed to spawn worker", outcome="spawn_failed",
    failure_limit=1, release_claim=True, end_run=True,
)
check(
    "9. spawn-path trip is floored too",
    state(conn, "spawn") == ("blocked", DISPATCHER_LIMIT),
    f"got {state(conn, 'spawn')}, want ('blocked', {DISPATCHER_LIMIT})",
)
spawn_row = conn.execute(
    "SELECT claim_lock, worker_pid, current_run_id FROM tasks WHERE id = 'spawn'"
).fetchone()
check(
    "9. the spawn-path claim is still released and the run closed",
    spawn_row["claim_lock"] is None
    and spawn_row["worker_pid"] is None
    and spawn_row["current_run_id"] is None,
    f"claim_lock={spawn_row['claim_lock']!r} pid={spawn_row['worker_pid']!r} "
    f"run={spawn_row['current_run_id']!r}",
)
promoted = K.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "9. spawn-path trip survives recompute_ready",
    state(conn, "spawn")[0] == "blocked" and promoted == 0,
    f"state {state(conn, 'spawn')}, promoted {promoted}",
)

# --- 3: no inflation on the ordinary path -----------------------------------
make(conn, "normal")
K._record_task_failure(
    conn, "normal", error="boom 1", outcome="crashed",
    failure_limit=DISPATCHER_LIMIT,
)
check(
    "3. the first ordinary failure neither blocks nor inflates",
    state(conn, "normal") == ("ready", 1),
    f"got {state(conn, 'normal')}, want ('ready', 1)",
)
K._record_task_failure(
    conn, "normal", error="boom 2", outcome="crashed",
    failure_limit=DISPATCHER_LIMIT,
)
check(
    "3. the ordinary trip stores the true count",
    state(conn, "normal") == ("blocked", DISPATCHER_LIMIT),
    f"got {state(conn, 'normal')}, want ('blocked', {DISPATCHER_LIMIT})",
)

# --- the audit trail must not change meaning --------------------------------
payload = gave_up_payload(conn, "proto")
check(
    "the gave_up payload still reports the true attempt count",
    payload.get("failures") == 1,
    f"payload: {payload}",
)
check(
    "the gave_up payload still reports the deciding threshold",
    payload.get("effective_limit") == PROTOCOL_LIMIT,
    f"payload: {payload}",
)

# Every arm, not just the one where the floor happens not to bite. Asserting
# recoverability on ``proto`` alone is vacuous: its effective_limit (3) already
# exceeds the floor, so the two formulas agree there and the systemic arm --
# the one that motivated this patch -- goes unchecked.
for tid in ("proto", "systemic", "override", "spawn"):
    payload = gave_up_payload(conn, tid)
    stored = state(conn, tid)[1]
    check(
        f"the stored counter is recoverable from {tid}'s untouched payload",
        stored_from_payload(payload) == stored,
        f"payload {payload} reconstructs to {stored_from_payload(payload)}, "
        f"column holds {stored}",
    )
check(
    "the systemic arm is the one that needs the two-arm formula",
    max(gave_up_payload(conn, "systemic").get("failures", 0),
        gave_up_payload(conn, "systemic").get("effective_limit", 0))
    != state(conn, "systemic")[1],
    "the naive one-line formula now agrees on the systemic arm, so the "
    "docstring's two-arm caveat has gone stale — re-derive it before trusting "
    "the simpler form",
)
conn.close()

# --- 4, 5, 6, 8: preservation. This is what the rejected fix broke. ---------
conn = board()

# 4. assign_task zeroes the counter on a profile change and calls that "an
#    explicit recovery action". It emits kind 'assigned', not 'unblocked', and
#    does not change status -- so any fix that reads the gave_up EVENT instead
#    of the counter pins this card in blocked forever.
make(conn, "reassigned")
K._record_task_failure(
    conn, "reassigned", error="pv", outcome="crashed",
    failure_limit=PROTOCOL_LIMIT, force_trip=True,
)
check(
    "4. the reassigned card starts out tripped",
    state(conn, "reassigned")[0] == "blocked",
)
K.assign_task(conn, "reassigned", "platform")
check(
    "4. assign_task still zeroes the counter",
    state(conn, "reassigned")[1] == 0,
    f"state {state(conn, 'reassigned')}",
)
promoted = K.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "4. assign_task still recovers a force-tripped card",
    state(conn, "reassigned") == ("ready", 0) and promoted == 1,
    f"state {state(conn, 'reassigned')}, promoted {promoted}",
)

# 4b. The behaviour change operators have to know about, asserted rather than
#     only written down. assign_task only zeroes the counter when the assignee
#     actually CHANGES; to the same profile it just rewrites the column. That
#     was survivable before this patch because a systemic trip stored 1, below
#     the dispatcher's 2, so recompute_ready promoted the card whatever
#     assign_task did. Now the stored count is the exhausted budget and the
#     no-op assign no longer frees it. Reassign elsewhere, or unblock.
K._record_task_failure(
    conn, "reassigned", error="container OOMKilled", outcome="crashed",
    failure_limit=1,
)
check(
    "4b. the card is tripped again before the same-profile assign",
    state(conn, "reassigned") == ("blocked", DISPATCHER_LIMIT),
    f"got {state(conn, 'reassigned')}, want ('blocked', {DISPATCHER_LIMIT})",
)
K.assign_task(conn, "reassigned", "platform")  # same profile: a no-op reassign
promoted = K.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "4b. assign_task to the SAME profile no longer frees a tripped card",
    state(conn, "reassigned") == ("blocked", DISPATCHER_LIMIT) and promoted == 0,
    f"state {state(conn, 'reassigned')}, promoted {promoted} — if this now "
    "recovers, the counter stopped being the arbiter and the docstring's "
    "recovery-path list needs redoing",
)

# 5. unblock_task is the operator's exit.
make(conn, "unblocked")
K._record_task_failure(
    conn, "unblocked", error="pv", outcome="crashed",
    failure_limit=PROTOCOL_LIMIT, force_trip=True,
)
K.unblock_task(conn, "unblocked")
check(
    "5. unblock_task still recovers a force-tripped card",
    state(conn, "unblocked") == ("ready", 0),
    f"got {state(conn, 'unblocked')}, want ('ready', 0)",
)

# 6. A card gated only by a parent never trips, so it must still auto-promote.
make(conn, "parent", status="done")
make(conn, "child", status="todo")
conn.execute(
    "INSERT INTO task_links (parent_id, child_id) VALUES ('parent', 'child')"
)
conn.commit()
K.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "6. a parent-gated card with no trip still auto-promotes",
    state(conn, "child") == ("ready", 0),
    f"got {state(conn, 'child')}, want ('ready', 0)",
)

# 8. A worker/operator kanban_block stays sticky, as before. Note the counter
#    is 0 here, so only _has_sticky_block can hold this card.
make(conn, "sticky", status="blocked")
with K.write_txn(conn):
    K._append_event(conn, "sticky", "blocked", {"reason": "review-required"})
promoted = K.recompute_ready(conn, failure_limit=DISPATCHER_LIMIT)
check(
    "8. a worker kanban_block is still sticky",
    state(conn, "sticky")[0] == "blocked",
    f"state {state(conn, 'sticky')}, promoted {promoted}",
)

conn.close()

print()
if FAILURES:
    print(f"verify_kanban_scheduling: {len(FAILURES)} FAILED")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("verify_kanban_scheduling: all checks passed")
