"""Unit tests for the kanban scheduling patches applied by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

Three faults in ``hermes_cli/kanban_db.py``, one quartet, one test file:

  * the self-parenting dependency deadlock (``repair_inverted_dependencies``),
  * claims fenced to a process life, and the discriminator that decides which
    reclaims cost a retry (``release_dead_foreign_claims``),
  * the breaker counter, which is a pure source rewrite and so is tested
    through the applier alone.

The dependency tests run against a miniature of the real schema and, crucially,
against a copy of the *real* gating predicate that ``claim_task`` and
``recompute_ready`` share. A repair that leaves a card unclaimable is no repair
at all, so every one of them asserts on ``claimable()`` rather than on link rows.
A board carries an event log as well as tasks and links, because the repair
decides what it may touch from the order of the ``created`` and ``claimed`` rows
in ``task_events``: an edge is a fan-out of this card's run only if the child was
created after the card first started. ``fanned_out`` and ``planned`` spell the
two histories that matter — the worker that creates its own children, and the
planner that lays out a pipeline before anything runs.

The fencing tests are written in the vocabulary of the 2026-08-07 gateway
SIGBUS: the container restarted and kept its pod name, so the replacement
dispatcher adjudicated its predecessor's worker PIDs — every one of them
belonging to a process it never spawned — and abandoned six cards. OLD is the
process life that died, NEW is the one that came up, and OTHER_POD is a pod
Kubernetes replaced, which is the case the discriminator forgives.

The end-to-end consequences (a poison card reaching ``blocked``, a rollout
costing nothing, the breaker's own threshold being the one that decides) belong
to verify_kanban_scheduling.py, which drives the real engine inside the image.
What is pinned here is the contract these modules offer it.

The last section tests that verifier's own fixture rather than the patch. It
earns its place: a board-naming scheme there once silently stopped producing
fresh boards, and the resulting failure read for all the world like a defect in
``release_dead_foreign_claims``.
"""

import ast
import itertools
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from apply_kanban_scheduling import (
    BUILD_MARKER,
    CHARGE_ANCHOR,
    CRASH_BIND_ANCHOR,
    DEPENDENCY_ANCHOR,
    DEPENDENCY_PATCHED,
    EDITS,
    FENCE_ANCHOR,
    RELATIVE,
    SPAWN_BIND_ANCHOR,
    TRIP_ANCHOR,
    apply,
)
from apply_kanban_wake_nudge import (
    COMPLETE_ANCHOR as WAKE_COMPLETE_ANCHOR,
    CREATE_ANCHOR as WAKE_CREATE_ANCHOR,
    UNBLOCK_ANCHOR as WAKE_UNBLOCK_ANCHOR,
)
from kanban_scheduling import (
    CLAIM_TIME_UNKNOWN,
    CONCURRENT_OWNER,
    DEPENDENCY_EVENT_KIND,
    POD_REPLACED,
    PROCESS_DIED_IN_PLACE,
    RECLAIM_ERROR,
    RECLAIM_EVENT_KIND,
    Reclaimed,
    charge_reclaimed_cards,
    claim_host,
    claim_is_self,
    classify_reclaim,
    find_deadlocked_children,
    process_start_time,
    read_proc_start_time,
    release_dead_foreign_claims,
    repair_inverted_dependencies,
)
import kanban_scheduling

POD = "platform-agent-gateway-75b5f6ddf6-7dkd7"
OLD = f"{POD}:4"  # the dispatcher that took the bus error
NEW = f"{POD}:9"  # the one that replaced it, same pod name
OTHER_POD = "platform-agent-gateway-595bbd777f-5vlzk:7"

# A claim instant safely on either side of any plausible process start.
NOW = int(time.time())
PRE_BOOT = NOW - 86_400

# The fingerprint as ``hermes_cli/kanban_db.py`` ships it, and as this patch
# deliberately leaves it. Mirrored rather than imported because kanban_db only
# exists inside the image.
UPSTREAM_FINGERPRINT_SOURCE = (
    "def _error_fingerprint(error_text):\n"
    "    fp = re.sub(r'\\bpid \\d+\\b', 'pid N', error_text[:80])\n"
    "    fp = re.sub(r'\\b\\d{10,}\\b', '<TS>', fp)\n"
    "    return fp.lower().strip()\n"
)


def upstream_fingerprint(error_text):
    fp = re.sub(r"\bpid \d+\b", "pid N", error_text[:80])
    fp = re.sub(r"\b\d{10,}\b", "<TS>", fp)
    return fp.lower().strip()


# ---------------------------------------------------------------------------
# Part 1: inverted fan-out dependency edges
# ---------------------------------------------------------------------------

DEPENDENCY_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER
);
"""


def board(tasks, links=(), log=()):
    """A miniature board. ``log`` is the ``task_events`` history, in order.

    The log is inserted before the links so its ids are the low ones, matching
    a real board where an edge is written in the same transaction as the
    ``created`` event that records it.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DEPENDENCY_SCHEMA)
    for tid, status in tasks.items():
        conn.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", (tid, status))
    for task_id, kind in log:
        conn.execute(
            "INSERT INTO task_events (task_id, kind) VALUES (?, ?)", (task_id, kind)
        )
    for parent, child in links:
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (parent, child),
        )
    return conn


def fanned_out(parent, *children):
    """The history a worker writes: claimed first, then it creates its cards."""
    return [(parent, "created"), (parent, "claimed")] + [
        (child, "created") for child in children
    ]


def planned(*task_ids):
    """The history a planner writes: every card exists before anything is claimed."""
    return [(task_id, "created") for task_id in task_ids]


def claimable(conn, task_id):
    """The gate ``claim_task`` and ``recompute_ready`` both apply, verbatim."""
    undone = conn.execute(
        "SELECT 1 FROM task_links l "
        "JOIN tasks p ON p.id = l.parent_id "
        "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1",
        (task_id,),
    ).fetchone()
    return undone is None


def links_of(conn):
    return {
        (r["parent_id"], r["child_id"])
        for r in conn.execute("SELECT parent_id, child_id FROM task_links")
    }


def repair_events(conn, task_id):
    """The decoded payloads of whatever this module wrote about ``task_id``."""
    return [
        json.loads(r["payload"])
        for r in conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = ? ORDER BY id",
            (task_id, DEPENDENCY_EVENT_KIND),
        )
    ]


class RepairTest(unittest.TestCase):
    def test_the_reported_deadlock_is_broken(self):
        """t_ab112f5b's shape: three cards parented to the card that waits on them."""
        conn = board(
            {"P": "running", "A": "todo", "B": "todo", "C": "todo"},
            [("P", "A"), ("P", "B"), ("P", "C")],
            log=fanned_out("P", "A", "B", "C"),
        )
        for child in ("A", "B", "C"):
            self.assertFalse(claimable(conn, child), f"{child} starts deadlocked")

        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A", "B", "C"])

        for child in ("A", "B", "C"):
            self.assertTrue(claimable(conn, child), f"{child} must now dispatch")
        self.assertFalse(claimable(conn, "P"), "P must wait for its prerequisites")

    def test_waiting_card_becomes_claimable_once_children_finish(self):
        conn = board(
            {"P": "running", "A": "todo"}, [("P", "A")], log=fanned_out("P", "A")
        )
        repair_inverted_dependencies(conn, "P")
        self.assertFalse(claimable(conn, "P"))
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'A'")
        self.assertTrue(claimable(conn, "P"), "the wait resolves on its own")

    def test_settled_children_are_left_alone(self):
        """A finished child already satisfies the gate; inverting it would un-satisfy P."""
        conn = board(
            {"P": "running", "A": "done", "B": "archived"},
            [("P", "A"), ("P", "B")],
            log=fanned_out("P", "A", "B"),
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertTrue(claimable(conn, "P"))
        self.assertEqual(repair_events(conn, "P"), [])

    def test_legitimate_continuation_card_is_untouched(self):
        """Upstream's endorsed idiom: create a child, then complete yourself.

        The repair fires only from ``block_task``, so a creator that completes
        instead of blocking never reaches it and the continuation card runs on
        the original edge. ``ApplierTest`` pins the call site; this pins that the
        idiom needs no repair to work.
        """
        conn = board(
            {"P": "running", "K": "todo"}, [("P", "K")], log=fanned_out("P", "K")
        )
        self.assertFalse(claimable(conn, "K"))
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = 'P'")
        self.assertTrue(claimable(conn, "K"))

    def test_real_fan_in_is_not_disturbed(self):
        """P waits on A and B the correct way round: nothing to repair."""
        conn = board(
            {"P": "running", "A": "todo", "B": "running"},
            [("A", "P"), ("B", "P")],
            log=planned("A", "B", "P") + [("B", "claimed"), ("P", "claimed")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertFalse(claimable(conn, "P"))
        self.assertTrue(claimable(conn, "A"))

    # --- the run window: only cards this card fanned out are repaired --------

    def test_a_pipeline_stage_keeps_its_direction_when_it_blocks(self):
        """The bug this module shipped with, reproduced against the live engine.

        A is done, B was claimed, B -> C. Nothing here is a fan-out of B's run:
        C was planned before B ever started, so B's dependency block is about
        something else and the pipeline has to survive it. The parent-set guard
        this replaces said B had no unsettled parents — true of every card
        ``block_task`` accepts — and inverted B -> C, dispatching C ahead of the
        stage it exists to follow.
        """
        conn = board(
            {"A": "done", "B": "running", "C": "todo"},
            [("A", "B"), ("B", "C")],
            log=planned("A", "B", "C") + [("A", "claimed"), ("B", "claimed")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "B"), [])
        self.assertEqual(links_of(conn), {("A", "B"), ("B", "C")})
        self.assertFalse(claimable(conn, "C"), "C must still wait its turn")
        self.assertEqual(repair_events(conn, "B"), [])

    def test_a_fan_in_keeps_its_direction_after_one_input_finishes(self):
        """The same false positive in the fan-in shape.

        Once B is done it stops gating C, so "is this card the last unfinished
        parent" says yes and A's edge was inverted. C predates A's run, so the
        window says no.
        """
        conn = board(
            {"A": "running", "B": "done", "C": "todo"},
            [("A", "C"), ("B", "C")],
            log=planned("A", "B", "C") + [("B", "claimed"), ("A", "claimed")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "A"), [])
        self.assertEqual(links_of(conn), {("A", "C"), ("B", "C")})

    def test_a_card_that_was_never_claimed_repairs_nothing(self):
        """No ``claimed`` row means no run window, so the comparison is NULL.

        A raw ``UPDATE tasks SET status='running'`` is not evidence that the card
        fanned anything out. verify_kanban_scheduling.py builds this board.
        """
        conn = board(
            {"P": "running", "A": "todo"},
            [("P", "A")],
            log=[("P", "created"), ("A", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])

    def test_a_fan_out_from_a_run_that_died_is_still_repaired(self):
        """The watermark is the first claim, not the current one.

        A worker that creates A and is then killed leaves it as deadlocked as one
        that blocks. When the card is re-claimed, creates B and blocks for real,
        both children have to be released — matching ``claim_task``'s own
        ``started_at = COALESCE(started_at, ?)``, which likewise records the
        first start and not the latest.
        """
        conn = board(
            {"P": "running", "A": "todo", "B": "todo"},
            [("P", "A"), ("P", "B")],
            log=[
                ("P", "created"),
                ("P", "claimed"),
                ("A", "created"),
                ("P", "reclaimed"),
                ("P", "claimed"),
                ("B", "created"),
            ],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A", "B"])

    def test_find_deadlocked_children_ignores_children_older_than_the_run(self):
        conn = board(
            {"P": "running", "OLDER": "todo", "NEWER": "todo"},
            [("P", "OLDER"), ("P", "NEWER")],
            log=planned("P", "OLDER") + [("P", "claimed"), ("NEWER", "created")],
        )
        self.assertEqual(find_deadlocked_children(conn, "P"), ["NEWER"])

    def test_a_second_repair_finds_nothing_to_do(self):
        """The first pass turned the edge around, so the second finds no successor."""
        conn = board(
            {"P": "running", "A": "todo"}, [("P", "A")], log=fanned_out("P", "A")
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A"])
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])

    # --- inverting must actually free the child -----------------------------

    def test_child_with_another_unfinished_parent_is_left_alone(self):
        """Releasing one of two blockers frees nothing, so do not rewrite the edge."""
        conn = board(
            {"P": "running", "OTHER": "todo", "SINK": "todo"},
            [("P", "SINK"), ("OTHER", "SINK")],
            log=planned("P", "OTHER") + [("P", "claimed"), ("SINK", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertFalse(claimable(conn, "SINK"))

    def test_child_whose_other_parents_are_finished_is_repaired(self):
        conn = board(
            {"P": "running", "OLD": "done", "SINK": "todo"},
            [("P", "SINK"), ("OLD", "SINK")],
            log=planned("P", "OLD") + [("P", "claimed"), ("SINK", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["SINK"])
        self.assertTrue(claimable(conn, "SINK"))

    def test_mixed_graph_repairs_only_the_deadlocking_edges(self):
        """One board, all three reasons to leave an edge alone.

        DONE is settled, SHARED has another unfinished parent, and PRIOR was
        planned before P ever ran. Only A is a deadlocked fan-out of P's run.
        """
        conn = board(
            {"P": "running", "A": "todo", "DONE": "done", "SHARED": "todo",
             "OTHER": "todo", "PRIOR": "todo"},
            [("P", "A"), ("P", "DONE"), ("P", "SHARED"), ("OTHER", "SHARED"),
             ("P", "PRIOR")],
            log=planned("P", "OTHER", "PRIOR")
            + [("P", "claimed"), ("A", "created"), ("DONE", "created"),
               ("SHARED", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A"])
        self.assertTrue(claimable(conn, "A"))
        self.assertEqual(
            links_of(conn),
            {("A", "P"), ("P", "DONE"), ("P", "SHARED"), ("OTHER", "SHARED"),
             ("P", "PRIOR")},
        )

    # --- the cycle probe ----------------------------------------------------

    def test_cycle_is_refused_when_the_new_edge_is_the_one_that_closes_it(self):
        """T -> X -> C and T -> C, X settled. Inverting T->C would close a loop.

        This is the shape the probe exists for, and the only one that produces a
        cycle: the edge being *inserted* is C -> T, so what matters is whether T
        can still reach C after T -> C is dropped. Here it can, via X.

        Everything else passes first, which is what makes the probe load-bearing
        — C was created inside T's run and its only other parent (X) is ``done``,
        so C is genuinely deadlocked on T alone.
        """
        conn = board(
            {"T": "running", "X": "done", "C": "todo"},
            [("T", "X"), ("X", "C"), ("T", "C")],
            log=planned("T", "X") + [("T", "claimed"), ("C", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "T"), [])
        self.assertEqual(
            links_of(conn),
            {("T", "X"), ("X", "C"), ("T", "C")},
            "graph restored unchanged",
        )
        payload = repair_events(conn, "T")[0]
        self.assertEqual(payload["skipped_would_cycle"], ["C"])
        self.assertEqual(payload["inverted"], [])

    def test_a_pre_existing_cycle_through_the_gated_child_is_repaired(self):
        """P -> A -> M -> P, M ``done``. Inverting P->A breaks the loop, not makes one.

        The old probe walked from the child, which answered a question about the
        edge being deleted rather than the one being inserted, and refused this
        repair. Nothing here closes a loop: after P->A is dropped, P reaches
        nothing, so A -> P is safe and A becomes claimable.
        """
        conn = board(
            {"P": "running", "A": "todo", "M": "done"},
            [("P", "A"), ("A", "M"), ("M", "P")],
            log=planned("M", "P") + [("P", "claimed"), ("A", "created")],
        )
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["A"])
        self.assertEqual(links_of(conn), {("A", "M"), ("M", "P"), ("A", "P")})
        self.assertTrue(claimable(conn, "A"))
        payload = repair_events(conn, "P")[0]
        self.assertEqual(payload["inverted"], ["A"])
        self.assertEqual(payload["skipped_would_cycle"], [])

    def test_a_card_is_never_a_fan_out_of_its_own_run(self):
        """A self-link cannot pass the window: a card is created before it is claimed.

        ``link_tasks`` refuses ``parent_id == child_id`` outright, so this only
        arrives through raw SQL, and the window turns it away before the cycle
        probe has to.
        """
        conn = board({"P": "running"}, [("P", "P")], log=fanned_out("P"))
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])

    def test_reachability_walk_terminates_on_a_pre_existing_cycle(self):
        """n0..n49 is a ring. The walk must not spin looking for P.

        The ring is ``done`` apart from n0, so the last-unfinished-parent test
        lets n0 through — the point of the test is the 50-hop traversal.
        """
        tasks = {f"n{i}": "done" for i in range(50)}
        tasks["n0"] = "todo"
        tasks["P"] = "running"
        links = [(f"n{i}", f"n{i + 1}") for i in range(49)]
        links += [("n49", "n0"), ("P", "n0")]
        log = planned(*[f"n{i}" for i in range(1, 50)])
        log += [("P", "created"), ("P", "claimed"), ("n0", "created")]
        conn = board(tasks, links, log=log)
        # P is not reachable from the ring, so the edge inverts.
        self.assertEqual(repair_inverted_dependencies(conn, "P"), ["n0"])

    # --- the event ----------------------------------------------------------

    def test_no_children_writes_nothing(self):
        conn = board({"P": "running"}, log=fanned_out("P"))
        self.assertEqual(repair_inverted_dependencies(conn, "P"), [])
        self.assertEqual(repair_events(conn, "P"), [])

    def test_event_records_the_repair(self):
        conn = board(
            {"P": "running", "A": "todo"}, [("P", "A")], log=fanned_out("P", "A")
        )
        repair_inverted_dependencies(conn, "P", reason="waiting on cluster audits")
        payload = repair_events(conn, "P")[0]
        self.assertEqual(payload["inverted"], ["A"])
        self.assertEqual(payload["reason"], "waiting on cluster audits")

    def test_long_reason_is_clipped(self):
        conn = board(
            {"P": "running", "A": "todo"}, [("P", "A")], log=fanned_out("P", "A")
        )
        repair_inverted_dependencies(conn, "P", reason="x" * 5000)
        self.assertEqual(len(repair_events(conn, "P")[0]["reason"]), 200)

    def test_find_deadlocked_children_ignores_settled(self):
        conn = board(
            {"P": "running", "A": "todo", "B": "done", "C": "blocked"},
            [("P", "A"), ("P", "B"), ("P", "C")],
            log=fanned_out("P", "A", "B", "C"),
        )
        self.assertEqual(find_deadlocked_children(conn, "P"), ["A", "C"])


# ---------------------------------------------------------------------------
# Part 2: claims fenced to a process life
# ---------------------------------------------------------------------------

FENCE_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    worker_pid INTEGER,
    claim_lock TEXT,
    claim_expires INTEGER,
    current_run_id INTEGER,
    started_at INTEGER
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    status TEXT,
    outcome TEXT,
    started_at INTEGER,
    ended_at INTEGER,
    error TEXT
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER
);
"""


def fence_board(rows):
    """rows: (id, status, worker_pid, claim_lock[, claimed_at]).

    Each row gets a running run row. ``claimed_at`` defaults to NULL, which is
    what an un-migrated board or a card whose run row has gone looks like — and
    which the discriminator reads as ``CLAIM_TIME_UNKNOWN``, so the default is
    the conservative one.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(FENCE_SCHEMA)
    for row in rows:
        tid, status, pid, lock = row[:4]
        claimed_at = row[4] if len(row) > 4 else None
        cur = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) "
            "VALUES (?, 'running', ?)",
            (tid, claimed_at),
        )
        conn.execute(
            "INSERT INTO tasks (id, status, worker_pid, claim_lock, claim_expires, "
            "current_run_id) VALUES (?, ?, ?, ?, 9999999999, ?)",
            (tid, status, pid, lock, cur.lastrowid),
        )
    return conn


def task(conn, tid):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()


def run_row(conn, tid):
    return conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC", (tid,)
    ).fetchone()


def events(conn, tid):
    return [
        (r["kind"], json.loads(r["payload"]))
        for r in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
            (tid,),
        )
    ]


def released(swept):
    """Every id the sweep handed back, charged or not.

    Sorted because the split is what the dedicated tests below assert on; these
    ones are about *whether* a card came back, and the fixtures name their cards
    so that sorted order is board order.
    """
    return sorted(list(swept.chargeable) + list(swept.forgiven))


DEAD = lambda pid: False  # noqa: E731
ALIVE = lambda pid: True  # noqa: E731


class ClaimIsSelfTest(unittest.TestCase):
    def test_same_pod_different_process_is_not_self(self):
        """The whole bug in one assertion: upstream's prefix test said True here."""
        self.assertTrue(OLD.startswith(POD + ":"))
        self.assertFalse(claim_is_self(OLD, NEW))

    def test_exact_token_is_self(self):
        self.assertTrue(claim_is_self(NEW, NEW))

    def test_other_pod_is_not_self(self):
        self.assertFalse(claim_is_self(OTHER_POD, NEW))

    def test_empty_and_none_are_not_self(self):
        self.assertFalse(claim_is_self("", NEW))
        self.assertFalse(claim_is_self(None, NEW))

    def test_prefix_of_our_token_is_not_self(self):
        """`pod:4` must not match `pod:41` in either direction."""
        self.assertFalse(claim_is_self(f"{POD}:4", f"{POD}:41"))
        self.assertFalse(claim_is_self(f"{POD}:41", f"{POD}:4"))


class ClaimHostTest(unittest.TestCase):
    """``claim_host`` must split exactly the way upstream's host_prefix did."""

    def test_the_host_half_is_everything_before_the_first_colon(self):
        self.assertEqual(claim_host(OLD), POD)
        self.assertEqual(claim_host(NEW), POD)

    def test_a_hostname_containing_a_colon_splits_once_like_upstream(self):
        self.assertEqual(claim_host("weird:host:12"), "weird")

    def test_missing_and_empty_tokens_are_the_empty_host(self):
        self.assertEqual(claim_host(None), "")
        self.assertEqual(claim_host(""), "")

    def test_a_token_with_no_pid_is_all_host(self):
        self.assertEqual(claim_host("bare-hostname"), "bare-hostname")


class ProcessStartTimeTest(unittest.TestCase):
    """The boundary the discriminator draws its second test against."""

    def setUp(self):
        self.saved = kanban_scheduling._PROCESS_START
        self.addCleanup(setattr, kanban_scheduling, "_PROCESS_START", self.saved)

    def test_the_proc_reader_answers_exactly_when_proc_is_there(self):
        """Linux in the image, absent on a developer's machine; both are fine."""
        self.assertEqual(
            read_proc_start_time() is not None, Path("/proc/self/stat").exists()
        )

    def test_the_proc_reading_is_a_plausible_wall_clock_instant(self):
        value = read_proc_start_time()
        if value is None:
            self.skipTest("no /proc on this platform")
        self.assertLessEqual(value, time.time())
        self.assertGreater(value, time.time() - 86_400)

    def test_the_answer_is_in_the_past_and_stable(self):
        first = process_start_time()
        self.assertLessEqual(first, time.time())
        self.assertEqual(process_start_time(), first)

    def test_the_cache_is_keyed_by_pid_so_a_fork_recomputes(self):
        """A module-level constant is inherited verbatim across ``os.fork()``.

        Caching by PID is what stops a forked child reporting its parent's age,
        which would make every claim the parent made look pre-boot to the child.
        """
        kanban_scheduling._PROCESS_START = (os.getpid() + 1_000_000, 1.0)
        self.assertNotEqual(process_start_time(), 1.0)
        self.assertEqual(kanban_scheduling._PROCESS_START[0], os.getpid())

    def test_the_cached_value_for_this_pid_is_returned_verbatim(self):
        kanban_scheduling._PROCESS_START = (os.getpid(), 12345.5)
        self.assertEqual(process_start_time(), 12345.5)


class ClassifyReclaimTest(unittest.TestCase):
    """Which departures are infrastructure and which are faults."""

    START = 1_000_000.0

    def test_same_pod_is_an_in_place_death_however_old_the_claim(self):
        """A container restart keeps the pod name. That is the poison-card shape.

        Forgiving it would reopen the loop ``charge_reclaimed_cards`` exists to
        close: a card whose work kills the gateway is reclaimed by the process
        that comes up next, and its claim is always older than that process.
        """
        self.assertEqual(
            classify_reclaim(OLD, NEW, self.START - 5_000, self.START),
            PROCESS_DIED_IN_PLACE,
        )

    def test_same_pod_and_a_fresh_claim_is_still_an_in_place_death(self):
        self.assertEqual(
            classify_reclaim(OLD, NEW, self.START + 5_000, self.START),
            PROCESS_DIED_IN_PLACE,
        )

    def test_a_different_pod_with_an_older_claim_is_a_replacement(self):
        self.assertEqual(
            classify_reclaim(OTHER_POD, NEW, self.START - 1, self.START),
            POD_REPLACED,
        )

    def test_a_different_pod_claiming_after_we_started_is_a_live_neighbour(self):
        """A CLI elsewhere, or a second replica: we watched that owner die."""
        self.assertEqual(
            classify_reclaim(OTHER_POD, NEW, self.START + 1, self.START),
            CONCURRENT_OWNER,
        )

    def test_the_boundary_second_is_charged(self):
        """``claim_task`` writes whole seconds, so the start is truncated.

        A claim 0.4s after this process began records as the second below the
        fractional start; comparing against the truncated value keeps that
        second on the charged side, which is the safe direction.
        """
        self.assertEqual(
            classify_reclaim(OTHER_POD, NEW, 1_000_000, 1_000_000.9),
            CONCURRENT_OWNER,
        )

    def test_an_absent_claim_time_is_not_a_provable_replacement(self):
        self.assertEqual(
            classify_reclaim(OTHER_POD, NEW, None, self.START), CLAIM_TIME_UNKNOWN
        )

    def test_an_unparseable_claim_time_is_not_a_provable_replacement(self):
        self.assertEqual(
            classify_reclaim(OTHER_POD, NEW, "whenever", self.START),
            CLAIM_TIME_UNKNOWN,
        )

    def test_only_a_pod_replacement_escapes_the_charge(self):
        """The property the sweep depends on, stated once rather than inferred."""
        fates = {
            classify_reclaim(OLD, NEW, PRE_BOOT, self.START),
            classify_reclaim(OTHER_POD, NEW, self.START + 1, self.START),
            classify_reclaim(OTHER_POD, NEW, None, self.START),
        }
        self.assertNotIn(POD_REPLACED, fates)


class ReleaseDeadForeignClaimsTest(unittest.TestCase):
    def test_predecessors_dead_claims_come_back(self):
        conn = fence_board(
            [("t1", "running", 1044, OLD), ("t2", "running", 1046, OLD)]
        )
        swept = release_dead_foreign_claims(conn, NEW, DEAD)
        self.assertEqual(released(swept), ["t1", "t2"])
        for tid in ("t1", "t2"):
            row = task(conn, tid)
            self.assertEqual(row["status"], "ready")
            self.assertIsNone(row["claim_lock"])
            self.assertIsNone(row["claim_expires"])
            self.assertIsNone(row["worker_pid"])
            self.assertIsNone(row["current_run_id"])

    def test_the_card_comes_back_ready_not_todo(self):
        """``todo`` is how a card used to walk past the breaker.

        ``recompute_ready`` applies its failure-limit guard only to ``blocked``
        rows and promotes ``todo`` unconditionally, and
        ``_record_task_failure``'s trip branch is gated on
        ``status IN ('ready', 'running')`` — so a reclaim that lands in ``todo``
        can be neither stopped nor even counted.
        """
        conn = fence_board([("t1", "running", 1044, OLD)])
        release_dead_foreign_claims(conn, NEW, DEAD)
        self.assertEqual(task(conn, "t1")["status"], "ready")

    def test_release_is_reclaimed_not_crashed(self):
        """The run outcome names the infrastructure event, not a worker fault."""
        conn = fence_board([("t1", "running", 1044, OLD)])
        release_dead_foreign_claims(conn, NEW, DEAD)
        run = run_row(conn, "t1")
        self.assertEqual(run["status"], "reclaimed")
        self.assertEqual(run["outcome"], "reclaimed")
        self.assertEqual(run["error"], RECLAIM_ERROR)
        self.assertIsNotNone(run["ended_at"])
        kind, payload = events(conn, "t1")[0]
        self.assertEqual(kind, RECLAIM_EVENT_KIND)
        self.assertNotEqual(kind, "crashed")
        self.assertEqual(payload["stale_lock"], OLD)
        self.assertEqual(payload["worker_pid"], 1044)
        self.assertEqual(payload["reason"], "owner_process_gone")

    def test_our_own_claims_are_never_touched(self):
        conn = fence_board([("mine", "running", 2001, NEW)])
        self.assertEqual(released(release_dead_foreign_claims(conn, NEW, DEAD)), [])
        self.assertEqual(task(conn, "mine")["status"], "running")

    def test_a_live_foreign_worker_is_left_to_the_ttl(self):
        """This is what makes the sweep safe from a CLI process.

        A CLI has its own claimer id and sees the gateway's claims as foreign,
        but the gateway's workers are alive, so nothing is taken from them.
        """
        conn = fence_board([("gw", "running", 2001, OLD)])
        self.assertEqual(
            released(release_dead_foreign_claims(conn, OTHER_POD, ALIVE)), []
        )
        self.assertEqual(task(conn, "gw")["status"], "running")

    def test_rolled_pods_claims_come_back_without_waiting_out_the_ttl(self):
        """claim_expires is far in the future; the sweep does not consult it."""
        conn = fence_board([("t1", "running", 2017, OTHER_POD, PRE_BOOT)])
        self.assertEqual(released(release_dead_foreign_claims(conn, NEW, DEAD)), ["t1"])

    def test_only_running_rows_are_candidates(self):
        conn = fence_board([("d", "done", 1044, OLD), ("b", "blocked", 1045, OLD)])
        self.assertEqual(released(release_dead_foreign_claims(conn, NEW, DEAD)), [])

    def test_rows_without_a_pid_are_left_for_the_ttl(self):
        conn = fence_board([("t1", "running", None, OLD)])
        self.assertEqual(released(release_dead_foreign_claims(conn, NEW, DEAD)), [])
        self.assertEqual(task(conn, "t1")["status"], "running")

    def test_mixed_board_releases_only_the_dead_foreign_rows(self):
        conn = fence_board(
            [
                ("mine_live", "running", 3001, NEW),
                ("mine_dead", "running", 3002, NEW),
                ("prev_dead", "running", 1044, OLD),
                ("other_live", "running", 2001, OTHER_POD),
            ]
        )
        alive = {3001, 2001}
        swept = release_dead_foreign_claims(conn, NEW, lambda p: p in alive)
        self.assertEqual(released(swept), ["prev_dead"])
        # mine_dead is ours: detect_crashed_workers still owns that decision.
        self.assertEqual(task(conn, "mine_dead")["status"], "running")

    def test_sweep_is_idempotent(self):
        conn = fence_board([("t1", "running", 1044, OLD)])
        self.assertEqual(released(release_dead_foreign_claims(conn, NEW, DEAD)), ["t1"])
        self.assertEqual(released(release_dead_foreign_claims(conn, NEW, DEAD)), [])

    def test_non_integer_pid_is_skipped_not_fatal(self):
        conn = fence_board([("t1", "running", 1044, OLD)])
        conn.execute("UPDATE tasks SET worker_pid = 'x' WHERE id = 't1'")
        self.assertEqual(released(release_dead_foreign_claims(conn, NEW, DEAD)), [])

    def test_a_card_inside_the_grace_window_is_left_alone(self):
        """The sweep must not be stricter than the per-row loop it runs ahead of.

        A worker claimed one second ago may not be on ``/proc`` yet, so a dead
        PID reading proves nothing — for a foreign claim exactly as much as for
        our own.
        """
        conn = fence_board([("t1", "running", 1044, OLD)])
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = 't1'", (time.time(),))
        self.assertEqual(
            released(release_dead_foreign_claims(conn, NEW, DEAD, lambda: 30)), []
        )
        self.assertEqual(task(conn, "t1")["status"], "running")

    def test_a_card_past_the_grace_window_is_released(self):
        conn = fence_board([("t1", "running", 1044, OLD)])
        conn.execute(
            "UPDATE tasks SET started_at = ? WHERE id = 't1'", (time.time() - 31,)
        )
        self.assertEqual(
            released(release_dead_foreign_claims(conn, NEW, DEAD, lambda: 30)), ["t1"]
        )

    def test_a_null_started_at_does_not_confer_grace(self):
        """``fence_board()`` leaves ``started_at`` NULL, as an un-migrated row would."""
        conn = fence_board([("t1", "running", 1044, OLD)])
        self.assertEqual(
            released(release_dead_foreign_claims(conn, NEW, DEAD, lambda: 30)), ["t1"]
        )

    def test_the_grace_resolver_is_consulted_at_most_once(self):
        conn = fence_board(
            [("t1", "running", 1044, OLD), ("t2", "running", 1045, OLD)]
        )
        conn.execute("UPDATE tasks SET started_at = ?", (time.time() - 31,))
        calls = []

        def resolver():
            calls.append(1)
            return 30

        self.assertEqual(
            released(release_dead_foreign_claims(conn, NEW, DEAD, resolver)),
            ["t1", "t2"],
        )
        self.assertEqual(len(calls), 1)

    def test_an_already_closed_run_is_not_rewritten(self):
        conn = fence_board([("t1", "running", 1044, OLD)])
        conn.execute(
            "UPDATE task_runs SET ended_at = 123, outcome = 'completed' "
            "WHERE task_id = 't1'"
        )
        self.assertEqual(released(release_dead_foreign_claims(conn, NEW, DEAD)), ["t1"])
        row = run_row(conn, "t1")
        self.assertEqual(row["outcome"], "completed")
        self.assertEqual(row["ended_at"], 123)


class ReclaimDiscriminatorTest(unittest.TestCase):
    """Which of the sweep's releases the caller is told to charge for.

    The boundary is injected rather than read from ``/proc`` so these assert on
    the rule rather than on how fast the test suite runs.
    """

    START = float(NOW - 3_600)

    def sweep(self, rows, claimer=NEW):
        conn = fence_board(rows)
        return conn, release_dead_foreign_claims(
            conn, claimer, DEAD, None, self.START
        )

    def test_a_replaced_pods_card_is_returned_forgiven(self):
        conn, swept = self.sweep([("t1", "running", 1044, OTHER_POD, PRE_BOOT)])
        self.assertEqual(list(swept.forgiven), ["t1"])
        self.assertEqual(list(swept.chargeable), [])
        self.assertEqual(task(conn, "t1")["status"], "ready")

    def test_an_in_place_death_is_returned_chargeable(self):
        conn, swept = self.sweep([("t1", "running", 1044, OLD, PRE_BOOT)])
        self.assertEqual(list(swept.chargeable), ["t1"])
        self.assertEqual(list(swept.forgiven), [])

    def test_a_concurrent_owner_is_returned_chargeable(self):
        _, swept = self.sweep(
            [("t1", "running", 1044, OTHER_POD, int(self.START) + 60)]
        )
        self.assertEqual(list(swept.chargeable), ["t1"])

    def test_an_unknown_claim_time_is_returned_chargeable(self):
        _, swept = self.sweep([("t1", "running", 1044, OTHER_POD)])
        self.assertEqual(list(swept.chargeable), ["t1"])

    def test_the_two_halves_are_split_not_merged(self):
        conn, swept = self.sweep(
            [
                ("rolled", "running", 1044, OTHER_POD, PRE_BOOT),
                ("crashed", "running", 1045, OLD, PRE_BOOT),
            ]
        )
        self.assertIsInstance(swept, Reclaimed)
        self.assertEqual(list(swept.forgiven), ["rolled"])
        self.assertEqual(list(swept.chargeable), ["crashed"])

    def test_a_forgiven_card_is_released_exactly_like_a_charged_one(self):
        """Forgiveness is about the retry budget and nothing else.

        The card still comes back to ``ready``, the run is still closed with the
        reclaim outcome, and the event is still written — otherwise a free
        reclaim would be indistinguishable from a sweep that did nothing.
        """
        conn, _ = self.sweep([("t1", "running", 1044, OTHER_POD, PRE_BOOT)])
        row = task(conn, "t1")
        self.assertEqual(row["status"], "ready")
        self.assertIsNone(row["claim_lock"])
        self.assertIsNone(row["current_run_id"])
        run = run_row(conn, "t1")
        self.assertEqual(run["outcome"], "reclaimed")
        self.assertEqual(run["error"], RECLAIM_ERROR)

    def test_the_event_records_the_verdict_and_the_reason_for_it(self):
        conn, _ = self.sweep([("t1", "running", 1044, OTHER_POD, PRE_BOOT)])
        _, payload = events(conn, "t1")[0]
        self.assertEqual(payload["owner_fate"], POD_REPLACED)
        self.assertFalse(payload["charged"])

    def test_a_charged_release_says_so_on_the_board_too(self):
        """The counter and the history must not be able to disagree."""
        conn, _ = self.sweep([("t1", "running", 1044, OLD, PRE_BOOT)])
        _, payload = events(conn, "t1")[0]
        self.assertEqual(payload["owner_fate"], PROCESS_DIED_IN_PLACE)
        self.assertTrue(payload["charged"])

    def test_the_old_payload_keys_are_all_still_there(self):
        """Anything reading ``reclaimed`` events predates the discriminator."""
        conn, _ = self.sweep([("t1", "running", 1044, OTHER_POD, PRE_BOOT)])
        _, payload = events(conn, "t1")[0]
        self.assertEqual(payload["stale_lock"], OTHER_POD)
        self.assertEqual(payload["worker_pid"], 1044)
        self.assertEqual(payload["claimer"], NEW)
        self.assertEqual(payload["reason"], "owner_process_gone")
        self.assertTrue(payload["fenced"])

    def test_the_claim_instant_comes_from_the_current_run_not_the_card(self):
        """``tasks.started_at`` is the FIRST start the card ever had.

        It is ``COALESCE(started_at, ?)``, so a card first claimed last week and
        re-claimed a moment ago still reads ancient there. Judging the owner on
        it would forgive a claim made while we were watching.
        """
        conn = fence_board(
            [("t1", "running", 1044, OTHER_POD, int(self.START) + 60)]
        )
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = 't1'", (PRE_BOOT,))
        swept = release_dead_foreign_claims(conn, NEW, DEAD, None, self.START)
        self.assertEqual(list(swept.chargeable), ["t1"])

    def test_repeated_pod_replacements_never_exhaust_the_budget(self):
        """The arithmetic this exists for: DEFAULT_FAILURE_LIMIT is 2.

        Charging every reclaim parked a long-lived card in ``blocked`` on the
        second ordinary deploy it happened to sit through, and there was nothing
        for the human that summoned to do about it.
        """
        for cycle in range(6):
            conn = fence_board(
                [("t1", "running", 1044, f"pod-{cycle}:{cycle}", PRE_BOOT)]
            )
            swept = release_dead_foreign_claims(conn, NEW, DEAD, None, self.START)
            self.assertEqual(list(swept.chargeable), [], f"cycle {cycle}")

    def test_the_default_boundary_is_this_processs_own_start(self):
        """Omitting ``process_start`` must not silently forgive everything."""
        conn = fence_board([("t1", "running", 1044, OTHER_POD, NOW + 3_600)])
        swept = release_dead_foreign_claims(conn, NEW, DEAD)
        self.assertEqual(list(swept.chargeable), ["t1"])


class ChargeReclaimedCardsTest(unittest.TestCase):
    """``_record_task_failure`` lives in kanban_db, so record what it is handed."""

    def setUp(self):
        self.calls = []

    def recorder(self, trips=()):
        def record_failure(conn, task_id, **kwargs):
            self.calls.append((task_id, kwargs))
            return task_id in trips

        return record_failure

    def test_every_released_card_is_charged_one_failure(self):
        parked = charge_reclaimed_cards(None, ["t1", "t2"], self.recorder())
        self.assertEqual(parked, [])
        self.assertEqual([tid for tid, _ in self.calls], ["t1", "t2"])

    def test_the_cards_the_breaker_parked_are_returned(self):
        """``dispatch_once`` reports these as auto-blocked, so a human hears."""
        parked = charge_reclaimed_cards(
            None, ["t1", "t2", "t3"], self.recorder(trips={"t2"})
        )
        self.assertEqual(parked, ["t2"])

    def test_nothing_released_means_nothing_charged(self):
        self.assertEqual(charge_reclaimed_cards(None, [], self.recorder()), [])
        self.assertEqual(self.calls, [])

    def test_the_charge_uses_the_dispatchers_own_threshold(self):
        """No second retry budget: no ``failure_limit``, no ``force_trip``.

        ``_record_task_failure`` then resolves per-task ``max_retries``, else
        ``kanban.failure_limit``, else ``DEFAULT_FAILURE_LIMIT`` — the same
        ladder every other failure kind is judged on.
        """
        charge_reclaimed_cards(None, ["t1"], self.recorder())
        _, kwargs = self.calls[0]
        self.assertNotIn("failure_limit", kwargs)
        self.assertNotIn("force_trip", kwargs)

    def test_the_charge_neither_releases_a_claim_nor_ends_a_run(self):
        """The sweep already did both, inside its own transaction."""
        charge_reclaimed_cards(None, ["t1"], self.recorder())
        _, kwargs = self.calls[0]
        self.assertFalse(kwargs["release_claim"])
        self.assertFalse(kwargs["end_run"])

    def test_the_gave_up_event_says_why_the_card_was_parked(self):
        charge_reclaimed_cards(None, ["t1"], self.recorder(trips={"t1"}))
        _, kwargs = self.calls[0]
        self.assertEqual(kwargs["error"], RECLAIM_ERROR)
        self.assertEqual(kwargs["outcome"], RECLAIM_EVENT_KIND)
        self.assertEqual(
            kwargs["event_payload_extra"],
            {"reason": "owner_process_gone", "fenced": True},
        )

    def test_only_the_chargeable_half_is_ever_handed_over(self):
        """The wiring the applier emits, asserted as a rule rather than a string.

        ``charge_reclaimed_cards`` has no idea a forgiven list exists; the sweep
        is what decides, and the caller passes ``.chargeable``. If that ever
        becomes the whole ``Reclaimed`` tuple again, the forgiven cards are
        charged and the discriminator is a no-op.
        """
        swept = Reclaimed(chargeable=["c1"], forgiven=["f1"])
        charge_reclaimed_cards(None, swept.chargeable, self.recorder())
        self.assertEqual([tid for tid, _ in self.calls], ["c1"])


class FingerprintTest(unittest.TestCase):
    """Why ``_error_fingerprint`` is left exactly as upstream ships it."""

    def test_normalising_the_pid_is_what_makes_a_burst_detectable(self):
        """Every message on this path is PID-prefixed, so the sub is load-bearing.

        ``detect_crashed_workers`` builds all three of these itself. Without the
        substitution no two concurrent workers can share a bucket, ``_fp_counts``
        never reaches the ``>= 3`` the systemic heuristic tests, and the
        detector is dead code.
        """
        for template in (
            "pid {} not alive",
            "pid {} exited with code 137",
            "pid {} killed by signal 9",
        ):
            messages = [template.format(p) for p in (1044, 1045, 1046, 1047)]
            with self.subTest(template=template):
                self.assertEqual(
                    len({upstream_fingerprint(m) for m in messages}),
                    1,
                    "four workers felled by one event must land in one bucket",
                )
                self.assertEqual(
                    len({m[:80] for m in messages}),
                    4,
                    "and the unnormalised text is what used to split them",
                )

    def test_distinct_faults_still_keep_their_own_buckets(self):
        self.assertNotEqual(
            upstream_fingerprint("pid 1044 exited with code 137"),
            upstream_fingerprint("pid 1044 killed by signal 9"),
        )

    def test_timestamp_normalisation_is_untouched(self):
        self.assertEqual(
            upstream_fingerprint("failed at 1754539230"),
            upstream_fingerprint("failed at 1754539999"),
        )

    def test_a_reclaim_never_reaches_the_fingerprint_at_all(self):
        """Six identical reclaim texts would otherwise read as one systemic fault.

        ``charge_reclaimed_cards`` runs its own loop precisely so that a
        container restart handing back every in-flight card cannot collapse into
        ``failure_limit=1`` and abandon the lot — the 2026-08-07 outcome by
        another route. The discriminator does not make that loop unnecessary: an
        in-place death is still charged, and still delivers every card at once
        with one identical error text.
        """
        self.assertEqual(
            len({upstream_fingerprint(RECLAIM_ERROR) for _ in range(6)}), 1
        )


# ---------------------------------------------------------------------------
# Part 3: the applier
# ---------------------------------------------------------------------------

# A stand-in for block_task's dependency branch with the same indentation as
# upstream, so the applier's ast.parse guard is exercised for real.
BLOCK_TASK_PREAMBLE = (
    "def block_task(conn, task_id, kind, reason, expected_run_id=None):\n"
    "    with write_txn(conn):\n"
    '        if kind == "dependency":\n'
    "            cur = conn.execute(SQL)\n"
    "            if cur.rowcount != 1:\n"
    "                return False\n"
    "            run_id = _end_run(conn, task_id)\n"
)
BLOCK_TASK_EPILOGUE = "            )\n            return True\n"

DETECT_CRASHED_PREAMBLE = (
    "\n\ndef detect_crashed_workers(conn):\n    with write_txn(conn):\n"
)
DETECT_CRASHED_MIDDLE = "            pass\n    auto_blocked = []\n"
DETECT_CRASHED_EPILOGUE = (
    "    detect_crashed_workers._last_auto_blocked = auto_blocked\n"
    "    return crashed\n\n"
)

# The shape of the region the breaker edits patch, reproduced from
# hermes_cli/kanban_db.py at the pinned Hermes version. Only the anchors
# matter, but the surroundings keep the fixture honest about indentation and
# about the two sibling UPDATEs in the else-branch that must NOT be touched.
RECORD_FAILURE_FIXTURE = '''\
def _record_task_failure(conn, task_id, error, *, outcome, failure_limit=None,
                        force_trip=False, release_claim=False, end_run=False,
                        event_payload_extra=None):
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    blocked = False
    with write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        failures = int(row["consecutive_failures"]) + 1
        task_override = (
            row["max_retries"] if "max_retries" in row.keys() else None
        )
        if task_override is not None:
            effective_limit = int(task_override)
            limit_source = "task"
        else:
            effective_limit = int(failure_limit)
            limit_source = "dispatcher"

        if force_trip or failures >= effective_limit:
            # Trip the breaker.
            if release_claim:
                # Spawn path: still running, also clear claim state.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('running', 'ready')",
                    (failures, error[:500], task_id),
                )
            else:
                # Timeout/crash path: task is already at ``ready``
                # with claim cleared; just flip to blocked + update
                # counter fields.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('ready', 'running')",
                    (failures, error[:500], task_id),
                )
            payload = {
                "failures": failures,
                "effective_limit": effective_limit,
                "limit_source": limit_source,
            }
            blocked = True
        else:
            # Below threshold. These two must keep binding the raw count.
            if release_claim:
                conn.execute(
                    "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (failures, error[:500], task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error[:500], task_id),
                )
    return blocked


'''


def pristine():
    """A fake ``kanban_db.py`` carrying exactly one of each of the six anchors."""
    return (
        "import re\n\n"
        + BLOCK_TASK_PREAMBLE
        + DEPENDENCY_ANCHOR
        + BLOCK_TASK_EPILOGUE
        + DETECT_CRASHED_PREAMBLE
        + FENCE_ANCHOR
        + DETECT_CRASHED_MIDDLE
        + CHARGE_ANCHOR
        + DETECT_CRASHED_EPILOGUE
        + RECORD_FAILURE_FIXTURE
        + UPSTREAM_FINGERPRINT_SOURCE
    )


class ApplierTest(unittest.TestCase):
    """The applier is the thing that fails the build, so exercise it directly."""

    def _tree(self, body):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        return root, target

    def _applied(self):
        root, target = self._tree(pristine())
        apply(root)
        return target.read_text()

    def test_the_fixture_is_a_faithful_stand_in(self):
        """Every anchor exactly once, and the whole thing parses as Python."""
        source = pristine()
        for label, anchor, _ in EDITS:
            with self.subTest(anchor=label):
                self.assertEqual(source.count(anchor), 1)
        ast.parse(source)

    def test_all_six_edits_land_and_the_result_parses(self):
        out = self._applied()
        self.assertIn("_kanban_repair_inverted_deps(conn, task_id, reason)", out)
        self.assertIn(
            "_kanban_reclaimed = _kanban_release_dead_foreign_claims(\n"
            "            conn, _kanban_claimer, _pid_alive, "
            "_resolve_crash_grace_seconds\n        )",
            out,
        )
        self.assertIn("_kanban_claim_is_self(lock, _kanban_claimer)", out)
        self.assertIn(
            "_kanban_charge_reclaimed_cards(\n"
            "            conn, _kanban_reclaimed.chargeable, _record_task_failure\n"
            "        )",
            out,
        )
        self.assertIn(BUILD_MARKER, out)
        ast.parse(out)

    def test_one_trailer_imports_everything_the_edits_call(self):
        """Three appliers used to write two trailers between them."""
        out = self._applied()
        self.assertEqual(out.count("from hermes_cli.kanban_scheduling import"), 1)
        for name in (
            "_kanban_charge_reclaimed_cards",
            "_kanban_claim_is_self",
            "_kanban_release_dead_foreign_claims",
            "_kanban_repair_inverted_deps",
        ):
            with self.subTest(name=name):
                self.assertIn(f"as {name},", out)

    def test_only_the_chargeable_half_is_passed_to_the_charge_loop(self):
        """Passing the whole tuple would charge the forgiven cards as well."""
        out = self._applied()
        self.assertIn("_kanban_reclaimed.chargeable, _record_task_failure", out)
        self.assertNotIn("conn, _kanban_reclaimed, _record_task_failure", out)

    def test_call_precedes_the_dependency_wait_event(self):
        out = self._applied()
        self.assertLess(
            out.index("_kanban_repair_inverted_deps"),
            out.index('"dependency_wait"'),
            "the repair must land before the event that reports the wait",
        )

    def test_host_prefix_comparison_is_gone_from_the_crash_reaper(self):
        self.assertNotIn("lock.startswith(host_prefix)", self._applied())

    def test_the_fingerprint_is_left_exactly_as_upstream_wrote_it(self):
        """The regression this file used to carry, pinned so it cannot return."""
        out = self._applied()
        self.assertIn(UPSTREAM_FINGERPRINT_SOURCE, out)
        self.assertNotIn("fp = error_text[:80]", out)

    def test_sweep_runs_before_the_rows_are_read(self):
        out = self._applied()
        self.assertLess(
            out.index("_kanban_release_dead_foreign_claims"),
            out.index('"SELECT id, worker_pid, claim_lock, started_at FROM tasks "'),
        )

    def test_the_charge_runs_after_the_sweeps_transaction_has_closed(self):
        """``_record_task_failure`` opens its own txn, and ``write_txn`` cannot nest."""
        out = self._applied()
        charge = out.index("_kanban_charge_reclaimed_cards")
        self.assertLess(out.index("with write_txn(conn):"), charge)
        self.assertLess(charge, out.index("_last_auto_blocked"))
        # Nothing the charge does may sit at the transaction's indentation.
        self.assertIn("\n    auto_blocked.extend(\n", out)

    def test_floors_the_persisted_counter_on_the_trip_path_only(self):
        patched = self._applied()

        # The floor landed, and it consults the per-task override.
        self.assertIn(BUILD_MARKER, patched)
        self.assertIn("if task_override is None:", patched)
        self.assertIn("persisted_failures, DEFAULT_FAILURE_LIMIT", patched)

        # Both blocked-branch UPDATEs now bind the floored value...
        self.assertEqual(
            patched.count("(persisted_failures, error[:500], task_id),"), 2
        )
        # ...and the two below-threshold UPDATEs still bind the raw count.
        self.assertEqual(patched.count("(failures, error[:500], task_id),"), 2)

        # The gave_up payload keeps reporting the true attempt count, so the
        # audit trail does not silently change meaning.
        self.assertIn('"failures": failures,', patched)

    def test_floor_is_computed_before_the_updates_that_use_it(self):
        patched = self._applied()
        assign = patched.index(BUILD_MARKER)
        first_use = patched.index("(persisted_failures, error[:500], task_id),")
        self.assertLess(assign, first_use)

    def test_every_anchor_is_load_bearing(self):
        """Remove any one of the six and the build must stop."""
        for label, anchor, _ in EDITS:
            with self.subTest(anchor=label):
                root, _ = self._tree(pristine().replace(anchor, "", 1))
                with self.assertRaises(SystemExit):
                    apply(root)

    def test_partial_apply_does_not_write(self):
        """One anchor missing: leave the file exactly as it was found."""
        body = pristine().replace(CHARGE_ANCHOR, "", 1)
        root, target = self._tree(body)
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual(target.read_text(), body)

    def test_duplicate_anchor_fails_the_build(self):
        for anchor in (FENCE_ANCHOR, TRIP_ANCHOR, SPAWN_BIND_ANCHOR,
                       CRASH_BIND_ANCHOR):
            with self.subTest(anchor=anchor.splitlines()[0]):
                root, _ = self._tree(pristine() + anchor)
                with self.assertRaises(SystemExit):
                    apply(root)

    def test_a_missing_target_file_fails_the_build(self):
        with self.assertRaises(SystemExit):
            apply(Path(tempfile.mkdtemp()))

    def test_the_patched_text_still_contains_the_anchor(self):
        """Which is exactly why counting the anchor cannot detect a re-run."""
        self.assertIn(DEPENDENCY_ANCHOR, DEPENDENCY_PATCHED)

    def test_a_second_run_is_refused_instead_of_stacking_the_call(self):
        """Replayed against the running gateway's kanban_db.py, the unguarded
        applier exited 0 three times in a row and left three copies of the call
        and three trailer imports behind.
        """
        root, target = self._tree(pristine())
        apply(root)
        once = target.read_text()
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual(target.read_text(), once, "a refused run must not write")


class WakeNudgeCompatibilityTest(unittest.TestCase):
    """``apply_kanban_wake_nudge`` rewrites the same file, immediately after.

    Its three ``kanban_db.py`` anchors sit in ``create_task``,
    ``complete_task`` and ``unblock_task`` — functions none of the six edits
    here touch. The coupling is invisible from either applier alone, so it is
    asserted rather than left to inspection: a future edit that widens an anchor
    into one of those functions fails here instead of in the image build.
    """

    # Each anchor is an indented fragment, so it only parses inside the shape
    # of the function it was cut from. The scaffolds reproduce just enough of
    # that shape for ast.parse to accept the combined fixture.
    WAKE_ANCHORS = (
        (
            "create_task",
            "\n\ndef create_task(conn, parents=None):\n"
            "    with write_txn(conn):\n"
            "        if True:\n"
            "            if parents:\n",
            WAKE_CREATE_ANCHOR,
        ),
        (
            "complete_task",
            "\n\ndef complete_task(conn, task_id):\n    pass\n",
            WAKE_COMPLETE_ANCHOR,
        ),
        (
            "unblock_task",
            "\n\ndef unblock_task(conn, task_id, new_status='ready'):\n"
            "    with write_txn(conn):\n",
            WAKE_UNBLOCK_ANCHOR,
        ),
    )

    def test_no_scheduling_anchor_overlaps_a_wake_nudge_anchor(self):
        for label, _, wake in self.WAKE_ANCHORS:
            for edit_label, anchor, _ in EDITS:
                with self.subTest(wake=label, edit=edit_label):
                    self.assertNotIn(anchor, wake)
                    self.assertNotIn(wake, anchor)

    def test_no_replacement_text_emits_a_wake_nudge_anchor(self):
        """Emitting one would give wake_nudge two matches and fail its count."""
        for label, _, wake in self.WAKE_ANCHORS:
            for edit_label, _, patched in EDITS:
                with self.subTest(wake=label, edit=edit_label):
                    self.assertNotIn(wake, patched)

    def test_the_wake_anchors_survive_a_full_scheduling_apply(self):
        root = Path(tempfile.mkdtemp())
        target = root / RELATIVE
        target.parent.mkdir(parents=True, exist_ok=True)
        body = pristine() + "".join(
            scaffold + wake for _, scaffold, wake in self.WAKE_ANCHORS
        )
        ast.parse(body)
        target.write_text(body)
        apply(root)
        out = target.read_text()
        for label, _, wake in self.WAKE_ANCHORS:
            with self.subTest(wake=label):
                self.assertEqual(
                    out.count(wake), 1, "still exactly one match for wake_nudge"
                )


# ---------------------------------------------------------------------------
# Part 4: the verifier's own fixture
# ---------------------------------------------------------------------------

VERIFIER = Path(__file__).with_name("verify_kanban_scheduling.py")


def name_from_listing(directory):
    """The scheme that shipped and failed: index by how many files are here."""
    return directory / f"kanban{len(list(directory.iterdir()))}.db"


class FreshBoardNamingTest(unittest.TestCase):
    """``fresh()`` must actually hand back an empty board every time.

    The Cloud Build failure this pins looked like a discriminator bug -- a sweep
    returning one more chargeable card than the board had -- and was not. Every
    "fresh" board after a certain point was reopening one file, so the sweep was
    correctly reporting a card an earlier section had left ``running``.

    The trigger is not reproducible off-Linux: ``K.connect`` opens each board in
    WAL mode, and whether closing the last connection unlinks the ``-wal`` and
    ``-shm`` sidecars depends on the SQLite build (it does in the image, it does
    not on macOS). So these tests do not try to reproduce WAL cleanup. They
    reproduce the property that made it fatal -- a name derived from a quantity
    that can go *down* -- which is portable, and is the thing that was wrong.
    """

    def test_naming_from_a_directory_listing_hands_out_a_live_boards_name(self):
        """The transition measured inside the image, reduced to bare files.

        Three boards are open and each holds four files -- ``.db``, ``-wal``,
        ``-shm`` and an ``.init.lock`` -- so the directory holds twelve and the
        scheme picks ``kanban12``. Opening that board adds four more. Meanwhile
        two earlier connections have been dropped, and SQLite unlinks the
        ``-wal`` and ``-shm`` of each, taking four away. The count is unchanged,
        so the very next call hands out ``kanban12`` a second time -- a board
        that is open and already has cards on it.

        This is the test that would have caught it. The scheme is correct only
        while the directory grows monotonically, and that is an assumption
        about SQLite's sidecar handling which nothing stated or checked.
        """
        root = Path(tempfile.mkdtemp())

        def open_board(stem):
            for suffix in (".db", ".db-shm", ".db-wal", ".db.init.lock"):
                (root / f"{stem}{suffix}").touch()

        def close_board(stem):
            for suffix in (".db-shm", ".db-wal"):
                (root / f"{stem}{suffix}").unlink()

        for n in range(3):
            open_board(f"kanban{n}")
        self.assertEqual(len(list(root.iterdir())), 12)

        first = name_from_listing(root)
        self.assertEqual(first.name, "kanban12.db")
        open_board(first.stem)
        close_board("kanban1")
        close_board("kanban2")

        self.assertEqual(
            name_from_listing(root),
            first,
            "the scheme handed out a board that is open and already has cards",
        )

    def test_a_counter_survives_the_same_removals(self):
        """The replacement, exercised against the same hostile directory."""
        root = Path(tempfile.mkdtemp())
        counter = itertools.count()
        handed_out = []
        for _ in range(6):
            path = root / f"kanban{next(counter)}.db"
            handed_out.append(path)
            path.touch()
            (root / f"{path.name}-wal").touch()
            for stale in root.glob("*.db-wal"):
                if stale.name != f"{path.name}-wal":
                    stale.unlink()
        self.assertEqual(len(set(handed_out)), 6)

    def test_the_verifier_names_its_boards_from_a_counter(self):
        """Tied to the shipped file, so the property above is not hypothetical.

        The verifier imports ``hermes_cli`` at module scope and only exists
        inside the image, so this reads the source rather than importing it.
        """
        source = VERIFIER.read_text()
        self.assertIn("itertools.count()", source)
        # The name expression, not the prose: the docstring that explains the
        # failure necessarily quotes the scheme it replaced.
        self.assertIn('f"kanban{next(_BOARDS)}.db"', source)
        self.assertNotIn('f"kanban{len(list(TMP.iterdir()))}.db"', source)

    def test_the_verifier_refuses_a_board_that_is_not_empty(self):
        """The second half of the fix: fail loudly at the cause, not downstream.

        Without this, the next recurrence is once again a confusing assertion
        about a sweep result somewhere far from the actual mistake.
        """
        source = VERIFIER.read_text()
        self.assertIn("SELECT count(*) FROM tasks", source)
        self.assertIn("fixture defect", source)

    def test_every_verifier_board_comes_from_the_fresh_helper(self):
        """A stray ``K.connect`` would bypass both halves of the guard."""
        source = VERIFIER.read_text()
        connects = [
            line.strip()
            for line in source.splitlines()
            if "K.connect(" in line and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            connects,
            ['conn = K.connect(TMP / f"kanban{next(_BOARDS)}.db")'],
            "a board is being opened outside fresh()",
        )


if __name__ == "__main__":
    unittest.main()
