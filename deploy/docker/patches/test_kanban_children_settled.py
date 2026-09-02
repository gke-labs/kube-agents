"""Unit tests for the fan-out completion guard installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import os
import sqlite3
import tempfile
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
    require_children_settled,
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
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS task_links (
    parent_id TEXT NOT NULL,
    child_id  TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
"""


def board(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(BOARD_SCHEMA)
    conn.execute(
        "INSERT INTO tasks VALUES (?, 'running', NULL)", (DELEGATION_CARD,)
    )
    conn.commit()
    return conn


def add_card(conn, task_id, status="running"):
    conn.execute("INSERT INTO tasks VALUES (?, ?, NULL)", (task_id, status))
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
            "    with _conn() as conn:\n"
            "        if True:\n"
            "            new_task = kb.get_task(conn, new_tid)\n"
            "            subscribed = _maybe_auto_subscribe(conn, new_tid)\n"
            "            _kanban_inherit_worker_subs(conn, new_tid)\n"
            "    return new_tid\n",
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
        source = self.patched().replace(
            "_kanban_inherit_worker_subs", "_kanban_inherit_worker_subs_v2"
        )
        self.target.write_text(source, encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)


if __name__ == "__main__":
    unittest.main()
