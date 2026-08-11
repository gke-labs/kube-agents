"""The kanban store, plus the guard that keeps it the only board-storage caller.

The fixture helpers here are public on purpose: `test_kanban_notify_propagate.py`
imports them, so the schema this repository believes Hermes has is written down
once. A second copy would drift, and a drifted fixture makes both suites pass
against a board that no longer exists.
"""

import importlib
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent.absolute()))
store_mod = importlib.import_module("kanban_store")

# Mirrors the production kanban_notify_subs schema (hermes_cli/kanban_db.py).
NOTIFY_SUBS_SCHEMA = """
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

# The board's card table. Only the primary key matters to this repository, but a
# real board always has it, so the fixture must too -- otherwise every test would
# silently exercise the no-card-table degradation path instead of the real one.
TASKS_SCHEMA = """
CREATE TABLE tasks (
    id     TEXT PRIMARY KEY,
    title  TEXT,
    status TEXT
);
"""


def make_board(path: str, with_tasks: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(NOTIFY_SUBS_SCHEMA)
    if with_tasks:
        conn.executescript(TASKS_SCHEMA)
    conn.close()


def add_card(path: str, *task_ids: str) -> None:
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT OR IGNORE INTO tasks (id, title, status) VALUES (?, ?, 'todo')",
        [(t, f"card {t}") for t in task_ids],
    )
    conn.commit()
    conn.close()


def add_sub(path, task_id, platform="google_chat", chat_id="spaces/AAA",
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


def rows_for(path, task_id):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM kanban_notify_subs WHERE task_id = ?", (task_id,)
    ).fetchall()
    conn.close()
    return rows


class BoardResolution(unittest.TestCase):
    def test_an_explicit_path_wins_over_the_environment(self):
        s = store_mod.KanbanStore.from_env("/explicit.db", env={"HERMES_KANBAN_DB": "/env.db"})
        self.assertEqual("/explicit.db", s.db_path)

    def test_the_environment_supplies_the_path_when_none_is_given(self):
        s = store_mod.KanbanStore.from_env(env={"HERMES_KANBAN_DB": "/env.db"})
        self.assertEqual("/env.db", s.db_path)

    def test_no_path_anywhere_refuses_rather_than_guessing(self):
        # A store pointed at a guessed path writes rows nothing reads, which is
        # harder to notice than a refusal.
        with self.assertRaises(store_mod.BoardUnavailable):
            store_mod.KanbanStore.from_env(env={})

    def test_board_unavailable_is_still_a_file_not_found_error(self):
        # Callers written against the raw path catch FileNotFoundError.
        self.assertTrue(issubclass(store_mod.BoardUnavailable, FileNotFoundError))

    def test_unknown_card_is_still_a_value_error(self):
        self.assertTrue(issubclass(store_mod.UnknownCard, ValueError))


class Reads(unittest.TestCase):
    def test_card_exists_answers_true_and_false(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            add_card(db, "here")
            s = store_mod.KanbanStore(db)
            self.assertIs(True, s.card_exists("here"))
            self.assertIs(False, s.card_exists("elsewhere"))

    def test_a_board_with_no_card_table_answers_none_not_false(self):
        # Tri-state. Collapsing "cannot say" into "missing" would turn an
        # unrecognised board into one where every card is absent.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db, with_tasks=False)
            self.assertIsNone(store_mod.KanbanStore(db).card_exists("anything"))

    def test_subscriptions_for_returns_only_the_identity_columns(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            add_sub(db, "p")
            identities = store_mod.KanbanStore(db).subscriptions_for("p")
            self.assertEqual(1, len(identities))
            self.assertEqual(
                set(store_mod.SUBSCRIPTION_IDENTITY_COLUMNS), set(identities[0])
            )
            self.assertNotIn("last_event_id", identities[0])

    def test_a_card_with_no_subscriptions_reads_as_empty(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            self.assertEqual([], store_mod.KanbanStore(db).subscriptions_for("p"))

    def test_a_missing_board_raises_rather_than_reading_empty(self):
        with self.assertRaises(store_mod.BoardUnavailable):
            store_mod.KanbanStore("/nonexistent/kanban.db").subscriptions_for("p")


class Writes(unittest.TestCase):
    def test_added_subscriptions_start_their_event_cursor_at_zero(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            s = store_mod.KanbanStore(db)
            s.add_subscriptions("c", [{"platform": "google_chat", "chat_id": "spaces/A",
                                       "thread_id": "t", "user_id": "u",
                                       "notifier_profile": "default"}])
            self.assertEqual(0, rows_for(db, "c")[0]["last_event_id"])

    def test_adding_the_same_identity_twice_is_a_no_op(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            s = store_mod.KanbanStore(db)
            identity = [{"platform": "google_chat", "chat_id": "spaces/A",
                         "thread_id": "t", "user_id": "u", "notifier_profile": "default"}]
            self.assertEqual(1, s.add_subscriptions("c", identity))
            self.assertEqual(1, s.add_subscriptions("c", identity))

    def test_adding_nothing_reports_the_existing_count_without_writing(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            add_sub(db, "c")
            self.assertEqual(1, store_mod.KanbanStore(db).add_subscriptions("c", []))

    def test_copy_subscriptions_moves_every_identity(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            add_sub(db, "p", chat_id="spaces/A", thread_id="t1")
            add_sub(db, "p", chat_id="spaces/B", thread_id="t2")
            self.assertEqual(2, store_mod.KanbanStore(db).copy_subscriptions("p", "c"))
            self.assertEqual(2, len(rows_for(db, "c")))

    def test_copying_from_an_unsubscribed_card_writes_nothing(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            self.assertEqual(0, store_mod.KanbanStore(db).copy_subscriptions("p", "c"))
            self.assertEqual([], rows_for(db, "c"))

    def test_a_constraint_violating_identity_is_skipped_not_raised(self):
        # `INSERT OR IGNORE` is what makes re-subscribing idempotent, and it does
        # not distinguish "already there" from "violates NOT NULL": both are
        # skipped silently. Documented rather than changed -- this is the
        # behaviour that shipped, and the returned count still tells the caller
        # the truth about what the card ended up with.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            good = {"platform": "google_chat", "chat_id": "spaces/A",
                    "thread_id": "t", "user_id": "u", "notifier_profile": "default"}
            bad = dict(good, chat_id=None, thread_id="t2")  # chat_id is NOT NULL
            written = store_mod.KanbanStore(db).add_subscriptions("c", [good, bad])
            self.assertEqual(1, written)
            self.assertEqual(1, len(rows_for(db, "c")))


class SchemaAssumptions(unittest.TestCase):
    def test_every_column_the_store_names_exists_on_a_real_board(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            missing = store_mod.KanbanStore(db).missing_columns(
                store_mod.NOTIFY_SUBS_TABLE, store_mod.SUBSCRIPTION_IDENTITY_COLUMNS
            )
            self.assertEqual([], missing)

    def test_missing_columns_names_what_is_absent(self):
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            self.assertEqual(
                ["invented"],
                store_mod.KanbanStore(db).missing_columns(
                    store_mod.NOTIFY_SUBS_TABLE, ["platform", "invented"]
                ),
            )

    def test_the_busy_timeout_comes_from_exactly_one_place(self):
        # Setting `timeout=` on connect() AND a `PRAGMA busy_timeout` silently
        # resolves to whichever ran last, so the source can advertise a wait the
        # connection does not honour.
        with TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "kanban.db")
            make_board(db)
            conn = store_mod.KanbanStore(db).connect()
            try:
                self.assertEqual(
                    store_mod.BUSY_TIMEOUT_SECONDS * 1000,
                    conn.execute("PRAGMA busy_timeout").fetchone()[0],
                )
                # journal_mode is the gateway's to choose; this store must not force it.
                self.assertEqual(
                    "delete", conn.execute("PRAGMA journal_mode").fetchone()[0]
                )
            finally:
                conn.close()


class NoDirectBoardAccessOutsideTheStore(unittest.TestCase):
    """The ratchet this milestone exists to set.

    "Direct SQLite writes: 0" is only true for as long as nobody adds the next
    one, and the next one will look reasonable in review. This makes it fail a
    test instead.
    """

    # Ships *inside* /opt/hermes as part of a patch set, so it is Hermes' own code
    # by the time it runs and dies with the patch set it belongs to. Excluded here
    # rather than allowlisted per-file so a new patch does not have to edit this.
    EXEMPT_DIRS = ("deploy/docker/patches",)

    # This module and its own test necessarily do both.
    EXEMPT_FILES = (
        "agents/platform/scripts/kanban_store.py",
        "agents/platform/scripts/test_kanban_store.py",
        "agents/platform/scripts/test_kanban_notify_propagate.py",
    )

    BOARD_TABLES = ("kanban_notify_subs",)

    def _repo_root(self) -> Path:
        root = Path(__file__).resolve().parents[3]
        self.assertTrue(
            (root / "AGENTS.md").is_file(),
            f"expected the repository root at {root}; the scan would silently cover nothing",
        )
        return root

    def test_no_other_python_file_opens_the_board_itself(self):
        root = self._repo_root()
        offenders = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel.startswith(self.EXEMPT_DIRS) or rel in self.EXEMPT_FILES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "sqlite3" not in text:
                continue
            if any(table in text for table in self.BOARD_TABLES):
                offenders.append(rel)
        self.assertEqual(
            [],
            offenders,
            "these files reach into the kanban board's storage directly; "
            "call agents/platform/scripts/kanban_store.KanbanStore instead",
        )

    def test_the_scan_would_actually_catch_one(self):
        # Without this, a scan that walks the wrong root or matches nothing passes
        # identically to a clean repository.
        root = self._repo_root()
        candidates = [
            p for p in root.rglob("*.py")
            if "sqlite3" in p.read_text(encoding="utf-8", errors="replace")
            and any(t in p.read_text(encoding="utf-8", errors="replace")
                    for t in self.BOARD_TABLES)
        ]
        self.assertTrue(
            candidates,
            "the scan matched no files at all -- the pattern or the root is wrong",
        )


if __name__ == "__main__":
    unittest.main()
