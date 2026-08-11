"""The propagation policy: which cards, in which direction, and failing soft.

Board storage itself is `test_kanban_store.py`'s subject; the schema fixtures are
imported from there so this repository writes down what it believes Hermes' board
looks like exactly once.
"""

import importlib
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

# Import the module under test from this directory.
sys.path.insert(0, str(Path(__file__).parent.absolute()))
prop = importlib.import_module("kanban_notify_propagate")

from test_kanban_store import (  # noqa: E402
    add_card as _add_card,
    add_sub as _add_sub,
    make_board as _make_db,
    rows_for as _rows,
)


class TestPropagate(unittest.TestCase):
    def test_copies_parent_subscription_to_child_with_reset_cursor(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            _add_card(db, "parent-1", "child-1")
            _add_sub(db, "parent-1", last_event_id=42)

            written = prop.propagate(db, "parent-1", "child-1")
            self.assertEqual(written, 1)

            child = _rows(db, "child-1")
            self.assertEqual(len(child), 1)
            row = child[0]
            # Chat identity is copied verbatim...
            self.assertEqual(row["platform"], "google_chat")
            self.assertEqual(row["chat_id"], "spaces/AAA")
            self.assertEqual(row["thread_id"], "spaces/AAA/threads/T1")
            self.assertEqual(row["user_id"], "users/u1")
            self.assertEqual(row["notifier_profile"], "default")
            # ...but the delivery cursor resets so the child's events are delivered.
            self.assertEqual(row["last_event_id"], 0)

    def test_multiple_parent_subs_all_propagate(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            _add_card(db, "parent-1", "child-1")
            _add_sub(db, "parent-1", chat_id="spaces/AAA", thread_id="t1")
            _add_sub(db, "parent-1", chat_id="spaces/BBB", thread_id="t2")

            written = prop.propagate(db, "parent-1", "child-1")
            self.assertEqual(written, 2)
            self.assertEqual(len(_rows(db, "child-1")), 2)

    def test_idempotent(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            _add_card(db, "parent-1", "child-1")
            _add_sub(db, "parent-1")
            prop.propagate(db, "parent-1", "child-1")
            # Second run must not duplicate or raise.
            written = prop.propagate(db, "parent-1", "child-1")
            self.assertEqual(written, 1)
            self.assertEqual(len(_rows(db, "child-1")), 1)

    def test_no_parent_subscription_is_noop(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            _add_card(db, "parent-1", "child-1")
            # Parent has no subscription (request didn't originate from chat).
            written = prop.propagate(db, "parent-1", "child-1")
            self.assertEqual(written, 0)
            self.assertEqual(len(_rows(db, "child-1")), 0)

    def test_same_parent_and_child_is_noop(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            _add_card(db, "x-1")
            _add_sub(db, "x-1")
            self.assertEqual(prop.propagate(db, "x-1", "x-1"), 0)

    def test_unknown_child_card_writes_nothing(self):
        # A typo'd or stale --to must not leave a subscription row behind. Nothing
        # would ever remove it: the notifier unsubscribes only when a task turns
        # terminal, and delete_task returns early when no `tasks` row matches, so
        # its cascade never reaches kanban_notify_subs. The row would be scanned on
        # every notifier tick for the life of the board.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            _add_card(db, "parent-1")  # child-typo is NOT on the board
            _add_sub(db, "parent-1")

            with self.assertRaises(ValueError):
                prop.propagate(db, "parent-1", "child-typo")
            self.assertEqual(len(_rows(db, "child-typo")), 0)

    def test_unknown_child_card_stays_fail_soft_through_main(self):
        # The guard raises so tests and callers see a real failure, but the CLI
        # wrapper must still exit 0 — a bad --to cannot break the worker's flow.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            _add_card(db, "parent-1")
            _add_sub(db, "parent-1")

            rc = prop.main(["--to", "child-typo", "--from", "parent-1", "--db", db])
            self.assertEqual(rc, 0)
            self.assertEqual(len(_rows(db, "child-typo")), 0)

    def test_board_without_tasks_table_still_propagates(self):
        # Documented degradation, and it is the store's tri-state card_exists
        # that makes it expressible: `None` means the board cannot answer, which
        # this module treats differently from `False`. If Hermes ever renames or
        # drops the card table, losing every propagation would be a worse bug
        # than the orphan row the guard prevents, so the check is skipped rather
        # than fatal.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db, with_tasks=False)
            _add_sub(db, "parent-1")

            self.assertEqual(prop.propagate(db, "parent-1", "child-1"), 1)
            self.assertEqual(len(_rows(db, "child-1")), 1)

    def test_missing_db_raises(self):
        with self.assertRaises(FileNotFoundError):
            prop.propagate("/nonexistent/kanban.db", "p", "c")

    def test_main_is_fail_soft_on_error(self):
        # main() must never propagate an exception up (would break the worker).
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")  # does not exist
            rc = prop.main(["--to", "child-1", "--from", "parent-1", "--db", db])
            self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
