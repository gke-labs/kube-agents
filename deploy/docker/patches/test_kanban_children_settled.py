"""Unit tests for the fan-out completion guard installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import kanban_children_settled as kcs
from kanban_children_settled import (
    CHILDREN_TABLE,
    NUDGE_TTL_SECONDS,
    WORKER_TASK_ENV,
    maybe_record_worker_child,
    record_worker_child,
    refuse_idle_dependency_block,
    require_children_settled,
    require_steps_settled,
)

# The board shape from the 2026-08-27 capture (issue #1010): build
# 2093054394793725952, task capacity-pinned-pool-probe. The platform worker on
# t_470a97c5 fanned out one investigation card per cluster agent, then closed
# its own card with a dispatch receipt, which the gateway delivered as the
# final answer.
DELEGATION_CARD = "t_470a97c5"
FANOUT_CHILDREN = ("t_seed_a", "t_seed_b", "t_seed_c")
RECEIPT = (
    "Fanned out cluster investigation tasks for `inference-server` to each "
    "cluster agent. Awaiting synthesis."
)

# The subset of hermes_cli/kanban_db.py's schema the guard touches. The guard's
# own table is deliberately absent: the module creates it on first write, and
# a board that predates the patch must read as "no children" (fail-open).
BOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT, result TEXT, current_run_id INTEGER, assignee TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS task_links (
    parent_id TEXT NOT NULL,
    child_id  TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE IF NOT EXISTS task_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind    TEXT NOT NULL
);
"""


def board(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(BOARD_SCHEMA)
    conn.execute(
        "INSERT INTO tasks (id, status, result) VALUES (?, 'running', NULL)", (DELEGATION_CARD,)
    )
    conn.commit()
    return conn


def add_card(conn, task_id, status="running", created_at=None):
    conn.execute(
        "INSERT INTO tasks (id, status, result, created_at) VALUES (?, ?, NULL, ?)",
        (task_id, status, int(time.time()) if created_at is None else created_at),
    )
    conn.commit()


def spawn(conn, child_id, status="running", creator=DELEGATION_CARD):
    """A worker on ``creator`` fans out ``child_id``, as the patched
    kanban_create handler records it."""
    add_card(conn, child_id, status)
    with mock.patch.dict(os.environ, {WORKER_TASK_ENV: creator}):
        maybe_record_worker_child(conn, child_id)


class Fixture(unittest.TestCase):
    def setUp(self):
        # File-backed rather than :memory:. The gate closes the connection
        # connect() hands it — in production every call gets a fresh one from
        # hermes_cli.kanban_db.connect — so the fixture board has to survive
        # the gate's close.
        self.path = str(Path(tempfile.mkdtemp()) / "kanban.db")
        self.conn = board(self.path)
        self.addCleanup(self.conn.close)
        kcs._refused_at.clear()
        self.addCleanup(kcs._refused_at.clear)

    def gate(self, task_id=DELEGATION_CARD, result=None):
        return require_children_settled(
            task_id, lambda: sqlite3.connect(self.path), result
        )

    def stored_result(self, task_id=DELEGATION_CARD):
        row = self.conn.execute(
            "SELECT result FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return row[0]


class TestTheIncidentShape(Fixture):
    def test_the_1010_receipt_completion_is_refused(self):
        """The exact t_470a97c5 shape: three live fan-out children, a
        dispatch receipt for a result. Before this patch the completion was
        accepted and the receipt was delivered as the final answer."""
        for child in FANOUT_CHILDREN:
            spawn(self.conn, child)
        err = self.gate()
        self.assertIsNotNone(err)
        for child in FANOUT_CHILDREN:
            self.assertIn(child, err)

    def test_the_refusal_tells_the_worker_what_to_do_instead(self):
        spawn(self.conn, FANOUT_CHILDREN[0])
        err = self.gate()
        # The error is part of the fix: the worker reads it mid-run and has to
        # be able to self-correct from the text alone.
        self.assertIn("kanban_show", err)
        self.assertIn("kanban_complete", err)
        self.assertIn("kanban_block", err)
        self.assertIn("result", err)
        self.assertIn("sleep", err)

    def test_a_completion_after_the_children_settle_is_accepted(self):
        for child in FANOUT_CHILDREN:
            spawn(self.conn, child, status="done")
        self.assertIsNone(self.gate())

    def test_archived_counts_as_settled(self):
        spawn(self.conn, "t_kid", status="archived")
        self.assertIsNone(self.gate())

    def test_a_card_with_no_children_is_untouched(self):
        self.assertIsNone(self.gate())


class TestTheRefusalPreservesTheSubmission(Fixture):
    """A refusal that returns before ``kb.complete_task`` writes nothing, so
    the refused ``result`` would otherwise exist only in the worker's context
    — the report-losing bug kanban_result_required's deleted shape check
    documents. This gate stashes the submission on the card before refusing,
    and accepts rather than refuse when it cannot."""

    def test_the_refused_result_is_stashed_on_the_card(self):
        spawn(self.conn, "t_kid")
        self.assertIsNotNone(self.gate(result=RECEIPT))
        self.assertEqual(self.stored_result(), RECEIPT)

    def test_the_card_stays_open_around_the_stash(self):
        spawn(self.conn, "t_kid")
        self.gate(result=RECEIPT)
        row = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (DELEGATION_CARD,)
        ).fetchone()
        self.assertEqual(row[0], "running")

    def test_the_refusal_says_the_submission_was_saved(self):
        spawn(self.conn, "t_kid")
        err = self.gate(result=RECEIPT)
        self.assertIn("saved on this card", err)

    def test_a_newer_submission_overwrites_an_older_stash(self):
        spawn(self.conn, "t_kid")
        self.gate(result="first draft")
        kcs._refused_at.clear()  # a fresh attempt window
        self.gate(result="second draft")
        self.assertEqual(self.stored_result(), "second draft")

    def test_a_blank_submission_stashes_nothing(self):
        spawn(self.conn, "t_kid")
        self.assertIsNotNone(self.gate(result="   "))
        self.assertIsNone(self.stored_result())

    def test_a_board_that_cannot_take_the_stash_is_not_refused(self):
        """Read-only board: the children query succeeds, the preserving write
        cannot — so the completion is accepted rather than refused, because a
        refusal that has not preserved the text risks losing it."""
        spawn(self.conn, "t_kid")
        ro = lambda: sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.assertIsNone(
            require_children_settled(DELEGATION_CARD, ro, RECEIPT)
        )
        self.assertIsNone(self.stored_result())

    def test_a_failed_stash_does_not_spend_the_nudge(self):
        """The accepted-on-stash-failure completion left no refusal on file,
        so a later attempt on a writable board still gets its nudge."""
        spawn(self.conn, "t_kid")
        ro = lambda: sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        require_children_settled(DELEGATION_CARD, ro, RECEIPT)
        self.assertEqual(kcs._refused_at, {})
        self.assertIsNotNone(self.gate(result=RECEIPT))


class TestTheContinuationExemption(Fixture):
    """Children gated on the completing card run only after it finishes.

    Refusing those would deadlock the board: upstream's own contract is
    "create a child of the current one (pass the current task id in
    ``parents``) ... then complete your own task", and the child cannot be
    claimed while its parent is unsettled (see kanban_scheduling.py).
    """

    def link(self, parent, child):
        self.conn.execute("INSERT INTO task_links VALUES (?, ?)", (parent, child))
        self.conn.commit()

    def test_a_follow_up_gated_on_this_card_does_not_block_completion(self):
        spawn(self.conn, "t_followup", status="todo")
        self.link(DELEGATION_CARD, "t_followup")
        self.assertIsNone(self.gate())

    def test_a_live_fanout_child_still_refuses_next_to_an_exempt_one(self):
        spawn(self.conn, "t_followup", status="todo")
        self.link(DELEGATION_CARD, "t_followup")
        spawn(self.conn, "t_live")
        err = self.gate()
        self.assertIsNotNone(err)
        self.assertIn("t_live", err)
        self.assertNotIn("t_followup", err)

    def test_a_link_from_someone_else_is_not_an_exemption(self):
        spawn(self.conn, "t_kid")
        self.link("t_other", "t_kid")
        self.assertIsNotNone(self.gate())


class TestTheNudgeIsNeverAWedge(Fixture):
    """One refusal per attempt window; the retry closes the card.

    Mirrors kanban_result_required: a gate that can refuse forever is a worse
    bug than the one it fixes. A worker at its turn limit, or one whose
    children are wedged, must still be able to close its card on the retry.
    """

    def test_the_immediate_retry_is_accepted_even_with_live_children(self):
        spawn(self.conn, "t_kid")
        self.assertIsNotNone(self.gate())
        self.assertIsNone(self.gate())

    def test_the_accepted_retry_spends_the_nudge(self):
        spawn(self.conn, "t_kid")
        self.gate()
        self.gate()
        self.assertEqual(kcs._refused_at, {})

    def test_a_stale_refusal_earns_a_fresh_nudge(self):
        spawn(self.conn, "t_kid")
        with mock.patch.object(kcs.time, "monotonic", side_effect=[1000.0]):
            self.assertIsNotNone(self.gate())
        late = 1000.0 + NUDGE_TTL_SECONDS + 1
        with mock.patch.object(kcs.time, "monotonic", side_effect=[late, late]):
            self.assertIsNotNone(self.gate())

    def test_cards_do_not_spend_each_others_nudges(self):
        spawn(self.conn, "t_kid")
        add_card(self.conn, "t_other_parent")
        spawn(self.conn, "t_other_kid", creator="t_other_parent")
        self.assertIsNotNone(self.gate(DELEGATION_CARD))
        self.assertIsNotNone(self.gate("t_other_parent"))

    def test_a_clean_completion_clears_a_stale_refusal(self):
        spawn(self.conn, "t_kid")
        self.gate()
        self.conn.execute("UPDATE tasks SET status = 'done' WHERE id = 't_kid'")
        self.conn.commit()
        self.assertIsNone(self.gate())
        self.assertEqual(kcs._refused_at, {})


class TestScopeAndAttribution(Fixture):
    def test_only_this_cards_children_count(self):
        add_card(self.conn, "t_other_parent")
        spawn(self.conn, "t_other_kid", creator="t_other_parent")
        self.assertIsNone(self.gate(DELEGATION_CARD))

    def test_a_recorded_child_whose_card_vanished_is_ignored(self):
        spawn(self.conn, "t_kid")
        self.conn.execute("DELETE FROM tasks WHERE id = 't_kid'")
        self.conn.commit()
        self.assertIsNone(self.gate())

    def test_the_listing_is_capped(self):
        for i in range(kcs.MAX_LISTED_CHILDREN + 2):
            spawn(self.conn, f"t_kid_{i:02d}")
        err = self.gate()
        self.assertIsNotNone(err)
        self.assertEqual(err.count("t_kid_"), kcs.MAX_LISTED_CHILDREN)
        self.assertIn("2 more", err)


class TestRecording(Fixture):
    def rows(self):
        return self.conn.execute(
            f"SELECT child_id, creator_id FROM {CHILDREN_TABLE} ORDER BY child_id"
        ).fetchall()

    def test_a_worker_create_is_recorded(self):
        add_card(self.conn, "t_kid")
        with mock.patch.dict(os.environ, {WORKER_TASK_ENV: DELEGATION_CARD}):
            self.assertTrue(maybe_record_worker_child(self.conn, "t_kid"))
        self.assertEqual(
            [tuple(r) for r in self.rows()], [("t_kid", DELEGATION_CARD)]
        )

    def test_a_create_that_hands_back_an_existing_card_is_not_recorded(self):
        """A repeated idempotency_key returns an old card, here one cron made a day ago."""
        add_card(self.conn, "t_cron", status="done", created_at=int(time.time()) - 86400)
        with mock.patch.dict(os.environ, {WORKER_TASK_ENV: DELEGATION_CARD}):
            self.assertFalse(maybe_record_worker_child(self.conn, "t_cron"))
        # The table is created on the first write, so none was made.
        self.assertIsNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = ?", (CHILDREN_TABLE,)).fetchone())

    def test_a_non_worker_create_is_not_recorded(self):
        add_card(self.conn, "t_kid")
        env = {k: v for k, v in os.environ.items() if k != WORKER_TASK_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(maybe_record_worker_child(self.conn, "t_kid"))
        self.assertIsNone(self.gate())

    def test_a_blank_worker_env_is_not_recorded(self):
        add_card(self.conn, "t_kid")
        with mock.patch.dict(os.environ, {WORKER_TASK_ENV: "  "}):
            self.assertFalse(maybe_record_worker_child(self.conn, "t_kid"))

    def test_a_card_is_never_its_own_child(self):
        with mock.patch.dict(os.environ, {WORKER_TASK_ENV: DELEGATION_CARD}):
            self.assertFalse(maybe_record_worker_child(self.conn, DELEGATION_CARD))

    def test_an_idempotent_recreate_keeps_the_first_attribution(self):
        """kanban_create with a repeated idempotency_key hands back an existing
        card; the second record must not steal it for a different creator."""
        add_card(self.conn, "t_kid")
        record_worker_child(self.conn, "t_kid", DELEGATION_CARD)
        record_worker_child(self.conn, "t_kid", "t_other_parent")
        self.assertEqual(
            [tuple(r) for r in self.rows()], [("t_kid", DELEGATION_CARD)]
        )

    def test_recording_survives_a_broken_connection(self):
        dead = sqlite3.connect(":memory:")
        dead.close()
        with mock.patch.dict(os.environ, {WORKER_TASK_ENV: DELEGATION_CARD}):
            self.assertFalse(maybe_record_worker_child(dead, "t_kid"))


class TestFailOpen(Fixture):
    """A guard bug must never block a completion; see kanban_result_required's
    posture. Anything short of a positive 'live children exist' answer lets
    the completion through."""

    def test_a_board_that_predates_the_patch_reads_as_no_children(self):
        bare = sqlite3.connect(":memory:")
        self.addCleanup(bare.close)
        bare.executescript(BOARD_SCHEMA)
        self.assertIsNone(require_children_settled(DELEGATION_CARD, lambda: bare))

    def test_a_connect_that_raises_fails_open(self):
        def boom():
            raise sqlite3.OperationalError("database is locked")

        self.assertIsNone(require_children_settled(DELEGATION_CARD, boom))

    def test_a_dead_connection_fails_open(self):
        dead = sqlite3.connect(":memory:")
        dead.close()
        self.assertIsNone(require_children_settled(DELEGATION_CARD, lambda: dead))

    def test_a_blank_task_id_fails_open(self):
        self.assertIsNone(require_children_settled("", lambda: self.conn))
        self.assertIsNone(require_children_settled(None, lambda: self.conn))

    def test_the_gate_closes_the_connection_it_opened(self):
        """Every call gets a fresh connection from connect() and the gate owns
        it — even on the error path, or a gateway that runs for weeks leaks
        one handle per refused completion."""
        closed = []

        class Conn:
            def execute(self, *a):
                raise sqlite3.OperationalError("no such table")

            def close(self):
                closed.append(True)

        self.assertIsNone(require_children_settled(DELEGATION_CARD, Conn))
        self.assertTrue(closed)


class TestTheDependencyBlock(Fixture):
    """The #2704 duplicate (card t_b999714f, #250/#251): refused at
    completion, the worker waited with kanban_block(kind="dependency"). With
    no parent to wait on, upstream re-queued the card to a fresh worker that
    had no record of the first one's pull request."""

    def block(self, kind="dependency", task_id=DELEGATION_CARD):
        return refuse_idle_dependency_block(task_id, kind, self.conn)

    def test_a_dependency_block_with_only_fanned_out_children_is_refused(self):
        spawn(self.conn, "t_seed_b", status="blocked")
        refusal = self.block()
        self.assertIsNotNone(refusal)
        self.assertIn("t_seed_b", refusal)
        self.assertIn("needs_input", refusal)
        self.assertIn("kanban_show", refusal)

    def test_a_card_with_no_children_at_all_is_refused_too(self):
        refusal = self.block()
        self.assertIsNotNone(refusal)
        self.assertNotIn("(`", refusal)

    def test_the_refusal_is_not_rationed(self):
        spawn(self.conn, "t_seed_b")
        self.assertIsNotNone(self.block())
        self.assertIsNotNone(self.block())

    def test_other_kinds_are_untouched(self):
        spawn(self.conn, "t_seed_b")
        for kind in (None, "needs_input", "capability", "transient"):
            self.assertIsNone(self.block(kind=kind), kind)

    def test_an_unsettled_parent_is_something_to_wait_on(self):
        add_card(self.conn, "t_upstream", status="running")
        self.conn.execute(
            "INSERT INTO task_links VALUES (?, ?)", ("t_upstream", DELEGATION_CARD)
        )
        self.assertIsNone(self.block())

    def test_a_settled_parent_is_not(self):
        add_card(self.conn, "t_upstream", status="done")
        self.conn.execute(
            "INSERT INTO task_links VALUES (?, ?)", ("t_upstream", DELEGATION_CARD)
        )
        self.assertIsNotNone(self.block())

    def event(self, task_id, kind):
        self.conn.execute(
            "INSERT INTO task_events (task_id, kind) VALUES (?, ?)", (task_id, kind)
        )

    def link(self, parent, child):
        self.conn.execute("INSERT INTO task_links VALUES (?, ?)", (parent, child))

    def test_a_child_the_scheduling_repair_will_flip_is_something_to_wait_on(self):
        """A child created with parents=[<this card>] after the card was
        claimed, and gated by nothing else: kanban_scheduling flips that edge
        inside block_task, after this gate, so the card really waits."""
        self.event(DELEGATION_CARD, "claimed")
        add_card(self.conn, "t_fanned", status="todo")
        self.event("t_fanned", "created")
        self.link(DELEGATION_CARD, "t_fanned")
        self.assertIsNone(self.block())

    def test_a_successor_planned_before_the_card_started_is_not(self):
        """A pipeline successor (A -> B -> C, C filed before B was claimed) is
        never flipped, so B's dependency block would re-queue it at once."""
        add_card(self.conn, "t_next_stage", status="todo")
        self.event("t_next_stage", "created")
        self.event(DELEGATION_CARD, "claimed")
        self.link(DELEGATION_CARD, "t_next_stage")
        self.assertIsNotNone(self.block())

    def test_a_child_that_still_waits_on_another_card_is_not(self):
        """The aggregation-card shape: Agg has parents=[X, Y]. X blocking on it
        frees nothing while Y is unsettled, so the repair leaves the edge and X
        would re-queue."""
        add_card(self.conn, "t_sibling", status="running")
        self.event(DELEGATION_CARD, "claimed")
        add_card(self.conn, "t_agg", status="todo")
        self.event("t_agg", "created")
        self.link(DELEGATION_CARD, "t_agg")
        self.link("t_sibling", "t_agg")
        self.assertIsNotNone(self.block())

    def test_a_card_this_worker_no_longer_runs_is_left_to_upstream(self):
        """A released or reassigned claim: block_task reports the block
        didn't land, rather than the worker being told it holds the card."""
        self.conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (DELEGATION_CARD,))
        self.assertIsNone(self.block())

    def test_another_runs_card_is_left_to_upstream(self):
        self.conn.execute("UPDATE tasks SET current_run_id = 8 WHERE id = ?", (DELEGATION_CARD,))
        self.assertIsNone(refuse_idle_dependency_block(DELEGATION_CARD, "dependency", self.conn, 7))
        self.assertIsNotNone(refuse_idle_dependency_block(DELEGATION_CARD, "dependency", self.conn, 8))

    def test_the_refusal_says_a_linked_successor_runs_after_completion(self):
        add_card(self.conn, "t_next_stage", status="todo")
        self.event("t_next_stage", "created")
        self.event(DELEGATION_CARD, "claimed")
        self.link(DELEGATION_CARD, "t_next_stage")
        self.assertIn("complete this card and it starts", self.block())

    def test_a_read_error_lets_the_block_through(self):
        broken = mock.Mock()
        broken.execute.side_effect = sqlite3.OperationalError("database is locked")
        self.assertIsNone(refuse_idle_dependency_block(DELEGATION_CARD, "dependency", broken))

    def test_a_board_without_the_attribution_table_still_refuses(self):
        self.assertIsNotNone(self.block())

    def test_the_gate_neither_writes_nor_closes_the_handlers_connection(self):
        spawn(self.conn, "t_seed_b")
        self.block()
        self.conn.execute("SELECT 1").fetchone()
        self.assertFalse(self.conn.in_transaction)


STEP_CARD = "t_bb95580d"


class TestTheStepGate(unittest.TestCase):
    """#2935's card: the cluster was created, the install failed, and the
    worker closed the card ``done``. Steps declared on the card hold it."""

    SCHEMA = """
    CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, result TEXT, body TEXT, assignee TEXT);
    CREATE TABLE task_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
        author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL
    );
    """

    def setUp(self):
        self.path = str(Path(tempfile.mkdtemp()) / "kanban.db")
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.executescript(self.SCHEMA)
        self.clock = 1_000

    def card(self, body="", task_id=STEP_CARD):
        self.conn.execute(
            "INSERT INTO tasks (id, status, result, body, assignee) VALUES (?, 'running', NULL, ?, 'platform')",
            (task_id, body),
        )
        self.conn.commit()

    def comment(self, body, task_id=STEP_CARD, at=None, author="platform"):
        self.clock += 1
        self.conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, author, body, self.clock if at is None else at),
        )
        self.conn.commit()

    def gate(self, result="Created the cluster; Substrate install failed.", task_id=STEP_CARD):
        return require_steps_settled(task_id, lambda: sqlite3.connect(self.path), result)

    def stored_result(self):
        return self.conn.execute("SELECT result FROM tasks WHERE id = ?", (STEP_CARD,)).fetchone()[0]

    def test_the_incident_is_refused_and_names_the_failed_step(self):
        self.card("Provision a cluster with Agent Substrate.\nstep cluster: planned\nstep substrate: planned")
        self.comment("step cluster: done")
        self.comment("step substrate: failed - helm has no credentials")
        refused = self.gate()
        self.assertIn("kanban_complete refused", refused)
        self.assertIn("substrate (failed)", refused)
        self.assertNotIn("cluster (", refused)
        self.assertIn('kanban_block(kind="needs_input")', refused)

    def test_a_refusal_preserves_the_submitted_result(self):
        self.card("step substrate: planned")
        self.gate(result="the report")
        self.assertEqual("the report", self.stored_result())

    def test_every_step_done_completes(self):
        self.card("step cluster: planned\nstep substrate: planned")
        self.comment("step cluster: done")
        self.comment("step substrate: done")
        self.assertIsNone(self.gate())

    def test_a_card_with_no_steps_completes_as_before(self):
        self.card("Investigate why checkout-gateway is crashlooping. The next step: done by Friday.")
        self.comment("Looked at the logs; it is OOMKilled.")
        self.assertIsNone(self.gate())

    def test_a_step_the_creator_declared_holds_the_card_though_the_worker_never_mentions_it(self):
        self.card("step substrate: planned")
        self.comment("Cluster created; finishing up.")
        self.assertIn("substrate (planned)", self.gate())

    def test_a_step_only_the_worker_declared_holds_the_card_too(self):
        self.card("Provision a cluster.")
        self.comment("step cluster: planned")
        self.assertIn("cluster (planned)", self.gate())

    def test_the_latest_line_wins_in_both_directions(self):
        self.card("step cluster: planned")
        self.comment("step cluster: done")
        self.comment("step cluster: failed - the node pool was deleted under us")
        self.assertIn("cluster (failed)", self.gate())
        self.comment("step cluster: done")
        self.assertIsNone(self.gate())

    def test_a_same_second_tie_keeps_write_order(self):
        self.card("step cluster: planned")
        self.comment("step cluster: done", at=5_000)
        self.comment("step cluster: failed", at=5_000)
        self.assertIn("cluster (failed)", self.gate())

    def test_there_is_no_skipped(self):
        self.card("step substrate: planned")
        self.comment("step substrate: skipped - not needed after all")
        self.assertIn("substrate (planned)", self.gate())

    def test_spelling_is_forgiving_but_keys_are_slugs(self):
        self.card("- Step Cluster: Planned\n> step substrate : planned")
        self.comment("STEP cluster: DONE\nstep substrate: done")
        self.assertIsNone(self.gate())
        self.assertEqual({}, kcs.card_steps(self.conn, "nope"))
        self.card("step a sentence: planned", task_id="t_prose")
        self.assertEqual({}, kcs.card_steps(self.conn, "t_prose"))
        self.card("step x_y: planned\nstep /etc/passwd: planned", task_id="t_not_slugs")
        self.assertEqual({}, kcs.card_steps(self.conn, "t_not_slugs"))

    def test_prose_that_mentions_a_step_is_not_a_plan(self):
        self.card(
            "Step 1: Planned maintenance window is Sunday.\n"
            "Step two: failed pods are restarted by the controller.\n"
            "The next step: done by Friday."
        )
        self.assertEqual({}, kcs.card_steps(self.conn, STEP_CARD))
        self.assertIsNone(self.gate())

    def test_a_hedged_status_is_not_done_and_a_reason_is_allowed(self):
        self.card("step substrate: planned\nstep cluster: planned\nstep pool: planned")
        self.comment("step substrate: done-ish except helm failed")
        self.comment("1. step cluster: done (PR #12)\n2) step pool: done - labels set")
        self.assertEqual(
            {"substrate": "planned", "cluster": "done", "pool": "done"}, kcs.card_steps(self.conn, STEP_CARD)
        )

    def test_the_refusal_is_not_rationed(self):
        self.card("step substrate: planned")
        self.assertIsNotNone(self.gate())
        self.assertIsNotNone(self.gate())

    def test_only_the_assignees_comments_count(self):
        # The front door relays a thread reply verbatim on a needs_input card.
        self.card("step substrate: planned")
        self.comment("step substrate: failed - helm has no credentials")
        self.comment("step substrate: done", author="default")
        self.comment("step extra: planned", author="default")
        self.assertEqual({"substrate": "failed"}, kcs.card_steps(self.conn, STEP_CARD))
        self.assertIn("substrate (failed)", self.gate())

    def test_closing_punctuation_and_a_comma_reason_still_record_the_status(self):
        self.card("step a: planned.\nstep b: planned\nstep c: planned\nstep d: planned")
        self.comment("step a: done.\r\nstep b: done, see PR #12\nstep c: done; labels set\nstep d: done!")
        self.assertEqual({"a": "done", "b": "done", "c": "done", "d": "done"}, kcs.card_steps(self.conn, STEP_CARD))
        self.card("step e: done-ish\nstep f: planned-ish.", task_id="t_hedge")
        self.assertEqual({}, kcs.card_steps(self.conn, "t_hedge"))

    def test_markdown_a_model_writes_still_records_the_status(self):
        self.card("- [ ] step a: planned\n- [ ] step b: planned\nstep c: planned\nstep d: planned\nstep e: planned")
        self.comment("- [x] step a: done\n**step b: done**\nstep `c`: done\nstep d: `done`\nstep e: done \u2705")
        self.assertEqual(
            {"a": "done", "b": "done", "c": "done", "d": "done", "e": "done"}, kcs.card_steps(self.conn, STEP_CARD)
        )

    def test_the_refusal_gives_the_exact_line_and_writing_it_clears_the_step(self):
        self.card("step substrate: planned")
        refused = self.gate()
        self.assertIn('kanban_comment(body="step substrate: done")', refused)
        self.comment("step substrate: done")
        self.assertIsNone(self.gate())

    def test_a_step_the_requester_dropped_is_recorded_by_the_worker_and_closes(self):
        self.card("step cluster: planned\nstep substrate: planned")
        self.comment("step cluster: done")
        refused = self.gate()
        self.assertIn("dropped at the requester's request", refused)
        self.comment("skip Substrate, the cluster is enough", author="default")
        self.assertIsNotNone(self.gate())
        self.comment("step substrate: done - dropped at the requester's request")
        self.assertIsNone(self.gate())

    def test_another_cards_steps_do_not_count(self):
        self.card("Provision a cluster.")
        self.card("step other: planned", task_id="t_other")
        self.comment("step other: failed", task_id="t_other")
        self.assertIsNone(self.gate())

    def test_a_long_plan_is_named_with_a_count(self):
        self.card("\n".join(f"step s{i}: planned" for i in range(kcs.MAX_LISTED_STEPS + 3)))
        self.assertIn("and 3 more", self.gate())

    def test_fails_open(self):
        bare = str(Path(tempfile.mkdtemp()) / "bare.db")
        sqlite3.connect(bare).close()
        self.assertIsNone(require_steps_settled(STEP_CARD, lambda: sqlite3.connect(bare), "r"))

        def broken():
            raise sqlite3.OperationalError("database is locked")

        self.assertIsNone(require_steps_settled(STEP_CARD, broken, "r"))
        self.assertIsNone(require_steps_settled("", broken, "r"))

    def test_a_refusal_that_cannot_save_the_result_is_not_issued(self):
        self.card("step substrate: planned")
        ro = lambda: sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.assertIsNone(require_steps_settled(STEP_CARD, ro, "the report"))


class TestTheApplier(unittest.TestCase):
    """Drive apply_kanban_children_settled against a fixture tree."""

    def setUp(self):
        import apply_kanban_children_settled as applier

        self.applier = applier
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / applier.RELATIVE
        self.target.parent.mkdir(parents=True)
        from kanban_result_required import NEW_GATE

        self.target.write_text(
            "def _handle_complete(args):\n"
            "    tid = args.get('tid')\n"
            "    summary = args.get('summary')\n"
            "    result = args.get('result')\n"
            + NEW_GATE
            + "    return kb.complete_task(tid, summary=summary, result=result)\n"
            "\n"
            "def _handle_create(args):\n"
            "    with _board(args.get('board')) as (kb, conn):\n"
            "        new_tid = kb.create_task(conn, title=title)\n"
            "        landed = _fields(kb.get_task(conn, new_tid), _CREATED_FIELDS)\n"
            "        return _ok(task_id=new_tid, **landed, subscribed=_maybe_auto_subscribe(conn, new_tid))\n"
            "\n"
            "def _handle_block(args):\n"
            "    with _board(args.get('board')) as (kb, conn):\n"
            + applier.BLOCK_ANCHOR
            + "        return _ok_landed(kb, conn, tid, 'blocked', block_kind=kind)\n",
            encoding="utf-8",
        )

    def patched(self):
        return self.target.read_text(encoding="utf-8")

    def test_both_hooks_land_and_the_file_still_parses(self):
        self.applier.apply(self.tmp)
        out = self.patched()
        self.assertIn("tid, _kanban_children_connect, result", out)
        self.assertIn("_kanban_record_worker_child(conn, new_tid)", out)
        self.assertIn("from tools.kanban_children_settled import", out)
        import ast

        ast.parse(out)

    def test_the_children_gate_runs_before_the_result_gate(self):
        """Refusing the completion outright is the more fundamental answer
        than critiquing the result's emptiness, and must come first so the
        worker's one result-nudge is not spent on a completion that was never
        going to be accepted."""
        self.applier.apply(self.tmp)
        out = self.patched()
        self.assertLess(
            out.index("_require_children_settled"), out.index("_require_result")
        )

    def test_the_step_gate_runs_after_the_children_gate_and_before_the_result_gate(self):
        self.applier.apply(self.tmp)
        out = self.patched()
        self.assertIn("_require_steps_settled(\n        tid, _kanban_children_connect, result", out)
        self.assertLess(out.index("_require_children_settled("), out.index("_require_steps_settled("))
        self.assertLess(out.index("_require_steps_settled("), out.index("_require_result"))

    def test_the_dependency_gate_runs_just_before_block_task(self):
        self.applier.apply(self.tmp)
        out = self.patched()
        self.assertIn("tid, kind, conn, _worker_run_id(tid)", out)
        self.assertLess(
            out.index("_refuse_idle_dependency_block("),
            out.index("ok = kb.block_task("),
        )

    def test_a_missing_block_anchor_fails_the_build(self):
        source = self.patched().replace("ok = kb.block_task(", "ok = kb.block_task_v2(")
        self.target.write_text(source, encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)

    def test_a_second_run_refuses(self):
        self.applier.apply(self.tmp)
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)

    def test_a_missing_complete_anchor_fails_the_build(self):
        source = self.patched().replace("_require_result", "_require_result_v2")
        self.target.write_text(source, encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)

    def test_a_missing_create_anchor_fails_the_build(self):
        source = self.patched().replace("_CREATED_FIELDS", "_CREATED_FIELDS_V2")
        self.target.write_text(source, encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)

    def test_the_create_hook_stacks_with_auto_subscribe_in_either_order(self):
        """Both patches hook the same upstream line, so neither may depend on
        the other having run: kanban_auto_subscribe is a retirement candidate
        (upstream inherits the creator's subscriptions itself since
        v2026.9.14), and retiring it must not break this applier."""
        import apply_kanban_auto_subscribe as auto_subscribe

        for first, second in ((auto_subscribe, self.applier), (self.applier, auto_subscribe)):
            self.setUp()
            first.apply(self.tmp)
            second.apply(self.tmp)
            out = self.patched()
            read_back = out.index("landed = _fields(kb.get_task(conn, new_tid)")
            self.assertLess(read_back, out.index("_kanban_record_worker_child(conn, new_tid)"))
            self.assertLess(read_back, out.index("_kanban_inherit_worker_subs(conn, new_tid)"))
            self.assertLess(out.index("_kanban_record_worker_child(conn, new_tid)"), out.index("return _ok("))
            self.assertLess(out.index("_kanban_inherit_worker_subs(conn, new_tid)"), out.index("return _ok("))
            import ast

            ast.parse(out)


if __name__ == "__main__":
    unittest.main()
