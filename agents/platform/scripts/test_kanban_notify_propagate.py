import importlib
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

# Import the module under test from this directory.
sys.path.insert(0, str(Path(__file__).parent.absolute()))
prop = importlib.import_module("kanban_notify_propagate")


# Mirror the production kanban_notify_subs schema (hermes_cli/kanban_db.py). If
# Hermes drifts the columns the helper copies, this fixture + the module's
# _COPY_COLUMNS assertion below will catch it.
_SCHEMA = """
CREATE TABLE kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    notifier_profile TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);
"""

# The board's card table. Only the primary key matters here — the helper reads
# nothing else from it — but a real board always has this table, so the fixture
# must too, or the tests would exercise the no-`tasks` degradation path instead
# of the production one.
_TASKS_SCHEMA = """
CREATE TABLE tasks (
    id     TEXT PRIMARY KEY,
    title  TEXT,
    status TEXT
);
"""

# The board's event log, verbatim from the live board's `sqlite_master`. The
# helper reads only `MAX(id) WHERE task_id = ?`, but the same argument as for
# `tasks` applies with more force: without this table every test would take
# `_event_head`'s no-table fallback and the cursor seeding would go untested.
# `id` must stay AUTOINCREMENT — the head is a board-wide sequence, and a
# fixture that reused ids per task would not model the thing being asserted.
_TASK_EVENTS_SCHEMA = """
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
"""


def _make_db(path: str, with_tasks: bool = True, with_events: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    if with_tasks:
        conn.executescript(_TASKS_SCHEMA)
    if with_events:
        conn.executescript(_TASK_EVENTS_SCHEMA)
    conn.close()


def _add_events(path: str, task_id: str, *kinds: str) -> int:
    """Append events to a card and return the board's resulting head id."""
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, NULL, 1000)",
        [(task_id, k) for k in kinds],
    )
    conn.commit()
    head = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
    conn.close()
    return head


def _add_card(path: str, *task_ids: str) -> None:
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT OR IGNORE INTO tasks (id, title, status) VALUES (?, ?, 'todo')",
        [(t, f"card {t}") for t in task_ids],
    )
    conn.commit()
    conn.close()


def _add_sub(path, task_id, platform="google_chat", chat_id="spaces/AAA",
             thread_id="spaces/AAA/threads/T1", user_id="users/u1",
             notifier_profile="default", last_event_id=42):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, "
        "user_id, notifier_profile, created_at, last_event_id) VALUES (?,?,?,?,?,?,?,?)",
        (task_id, platform, chat_id, thread_id, user_id, notifier_profile, 1000, last_event_id),
    )
    conn.commit()
    conn.close()


def _rows(path, task_id):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM kanban_notify_subs WHERE task_id = ?", (task_id,)
    ).fetchall()
    conn.close()
    return rows


class TestPropagate(unittest.TestCase):
    def test_copies_the_chat_identity_but_not_the_parents_cursor(self):
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
            # ...but the cursor is the child's own, never the parent's 42,
            # which indexes the parent's event history and means nothing here.
            self.assertEqual(row["last_event_id"], 0)

    def test_a_child_with_no_events_yet_starts_at_zero(self):
        # The common case: propagate runs immediately after kanban_create, so
        # there is nothing to skip and nothing to replay either way.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            _add_card(db, "parent-1", "child-1")
            _add_sub(db, "parent-1")

            prop.propagate(db, "parent-1", "child-1")
            self.assertEqual(_rows(db, "child-1")[0]["last_event_id"], 0)

    def test_a_back_filled_card_does_not_replay_its_history(self):
        """The bug this seeding exists to stop.

        Back-filling onto a card that has already run is what this script is
        for, and a cursor of 0 makes the notifier's next tick post that whole
        history into the thread — including a completion line for work nobody
        just asked about. Upstream `add_notify_sub` snaps to the head for the
        same reason (a boot-time burst of 100+ messages, Hermes issue #29905).
        """
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            _add_card(db, "parent-1", "child-1")
            _add_sub(db, "parent-1")
            head = _add_events(
                db, "child-1", "created", "claimed", "progress", "completed"
            )

            prop.propagate(db, "parent-1", "child-1")
            self.assertEqual(_rows(db, "child-1")[0]["last_event_id"], head)

    def test_the_cursor_is_the_childs_head_not_the_boards(self):
        # task_events.id is a board-wide AUTOINCREMENT, so a sibling card's
        # later events must not drag the child's cursor past its own.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            _add_card(db, "parent-1", "child-1", "sibling-1")
            _add_sub(db, "parent-1")
            child_head = _add_events(db, "child-1", "created", "claimed")
            board_head = _add_events(db, "sibling-1", "created", "claimed")
            self.assertGreater(board_head, child_head)

            prop.propagate(db, "parent-1", "child-1")
            self.assertEqual(_rows(db, "child-1")[0]["last_event_id"], child_head)

    def test_a_board_without_task_events_still_propagates(self):
        # Same fail-soft posture as the missing-`tasks` path below: losing the
        # propagation costs the user every update for that sub-step, which is
        # strictly worse than starting the cursor at 0 on a board so odd it
        # has no event log at all.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db, with_events=False)
            _add_card(db, "parent-1", "child-1")
            _add_sub(db, "parent-1")

            self.assertEqual(prop.propagate(db, "parent-1", "child-1"), 1)
            self.assertEqual(_rows(db, "child-1")[0]["last_event_id"], 0)

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
        # Documented degradation. This module's contract is a dependency on
        # kanban_notify_subs alone; if Hermes ever renames or drops `tasks`,
        # losing every propagation would be a worse bug than the orphan row the
        # guard prevents, so the check is skipped rather than fatal.
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

    def test_copy_columns_match_schema(self):
        # Guardrail: every column the helper copies must exist in the table.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            conn = sqlite3.connect(db)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(kanban_notify_subs)")}
            conn.close()
            for c in prop._COPY_COLUMNS:
                self.assertIn(c, cols)

    def test_connect_sets_one_unambiguous_busy_timeout(self):
        # The busy timeout must come from exactly one place. Setting `timeout=` on
        # connect() AND a `PRAGMA busy_timeout` silently resolves to whichever ran
        # last, so the source can advertise a wait the connection does not honor.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            _make_db(db)
            conn = prop._connect(db)
            try:
                self.assertEqual(
                    conn.execute("PRAGMA busy_timeout").fetchone()[0], 10_000
                )
                # journal_mode is the gateway's to choose; this helper must not force it.
                self.assertEqual(
                    conn.execute("PRAGMA journal_mode").fetchone()[0], "delete"
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
