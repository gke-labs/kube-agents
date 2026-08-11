#!/usr/bin/env python3
# kanban_store.py - the one module in this repository that talks to the kanban
# board's storage.
#
# Why this exists
# ---------------
# The board is Hermes' SQLite file. Repository-owned code that opens it directly
# is coupled to three things at once: that the board is SQLite at all, where the
# file lives, and what Hermes calls its tables and columns. Spread across several
# callers, that coupling has to be found before it can be changed.
#
# So it lives here instead. Everything a caller needs to know is a method on
# `KanbanStore`; everything Hermes-specific -- the `HERMES_KANBAN_DB` env var, the
# `kanban_notify_subs` and `tasks` table names, the busy-timeout policy -- is a
# constant in this file. When the board stops being a SQLite file in a Hermes pod,
# this module is what changes, and callers keep their code.
#
# What this module deliberately does not do
# -----------------------------------------
# No logging, no `sys.exit`, no fail-soft. It raises and returns facts. Whether a
# failed board write should stop the caller is the caller's decision, and the
# helper that decides it fail-soft (`kanban_notify_propagate.py`) is a different
# file for exactly that reason.

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Iterable, Sequence

# --- what Hermes calls things -------------------------------------------------
#
# Pinned as constants rather than inlined in each query so a Hermes schema change
# is one edit here and a small number of failing tests, instead of a grep.

BOARD_PATH_ENV = "HERMES_KANBAN_DB"
NOTIFY_SUBS_TABLE = "kanban_notify_subs"
TASKS_TABLE = "tasks"

# The chat-identity columns of a subscription: the part that identifies *where* a
# notification goes. `task_id`, `created_at` and `last_event_id` are excluded
# because they are per-subscription bookkeeping the store manages itself.
SUBSCRIPTION_IDENTITY_COLUMNS: tuple[str, ...] = (
    "platform",
    "chat_id",
    "thread_id",
    "user_id",
    "notifier_profile",
)

# `timeout=` IS the busy timeout (Python passes it to sqlite3_busy_timeout), so it
# is the only knob set -- a `PRAGMA busy_timeout` would silently override it and
# leave two different values in the source. Waiting matters: the gateway and the
# CLI write this same board, and work lost to a busy DB is work the user never
# hears about. Deliberately no `PRAGMA journal_mode`: cooperate with the WAL store
# the gateway already set up, never force it.
BUSY_TIMEOUT_SECONDS = 10


class KanbanStoreError(Exception):
    """Base for every error this module raises."""


class BoardUnavailable(KanbanStoreError, FileNotFoundError):
    """The board file is not where it was expected.

    Also a `FileNotFoundError` because that is what callers written against the
    raw SQLite path already catch.
    """


class UnknownCard(KanbanStoreError, ValueError):
    """A task id that is not on the board."""


class KanbanStore:
    """Board access, scoped to one SQLite file.

    Construct with an explicit path, or with `from_env()` to take the path the
    dispatcher pins into every worker.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ---- construction --------------------------------------------------------

    @classmethod
    def from_env(cls, db_path: str | None = None, env: dict[str, str] | None = None) -> "KanbanStore":
        """Resolve the board path, preferring an explicit one.

        Raises `BoardUnavailable` when neither is set, rather than defaulting to a
        guessed path: a store pointed at the wrong file writes rows nothing reads,
        which is harder to notice than a refusal.
        """
        environ = os.environ if env is None else env
        path = db_path or environ.get(BOARD_PATH_ENV, "")
        if not path:
            raise BoardUnavailable(
                f"no board configured: pass a path or set ${BOARD_PATH_ENV}"
            )
        return cls(path)

    # ---- connection ----------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open the board. Callers outside this module should not need this."""
        if not os.path.exists(self.db_path):
            raise BoardUnavailable(f"kanban DB not found at {self.db_path!r}")
        conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        return conn

    def _has_table(self, conn: sqlite3.Connection, table: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )

    # ---- reads ---------------------------------------------------------------

    def card_exists(self, task_id: str) -> bool | None:
        """Is this card on the board?

        Three answers, not two: `None` means the board cannot say, because it has
        no `tasks` table. Callers decide what to do with that -- collapsing it into
        `False` would turn a board this module does not recognise into a board on
        which every card is missing.
        """
        conn = self.connect()
        try:
            if not self._has_table(conn, TASKS_TABLE):
                return None
            row = conn.execute(
                f"SELECT 1 FROM {TASKS_TABLE} WHERE id = ?", (task_id,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def subscriptions_for(self, task_id: str) -> list[dict[str, Any]]:
        """The chat identities subscribed to this card, newest schema fields only."""
        cols = ", ".join(SUBSCRIPTION_IDENTITY_COLUMNS)
        conn = self.connect()
        try:
            rows = conn.execute(
                f"SELECT {cols} FROM {NOTIFY_SUBS_TABLE} WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            return [{c: row[c] for c in SUBSCRIPTION_IDENTITY_COLUMNS} for row in rows]
        finally:
            conn.close()

    def subscription_count(self, task_id: str) -> int:
        conn = self.connect()
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM {NOTIFY_SUBS_TABLE} WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        finally:
            conn.close()

    # ---- writes --------------------------------------------------------------

    def add_subscriptions(
        self,
        task_id: str,
        identities: Sequence[dict[str, Any]],
        *,
        now: int | None = None,
    ) -> int:
        """Subscribe these chat identities to this card. Returns the resulting count.

        Idempotent: the subscription primary key is
        `(task_id, platform, chat_id, thread_id)`, so re-adding an identity is a
        no-op rather than a duplicate. `last_event_id` starts at 0 so the card's
        events are delivered from the beginning.

        `INSERT OR IGNORE` is what buys that idempotency, and it does not
        distinguish "already subscribed" from "violates a constraint" -- both are
        skipped without an error. The returned count is therefore the fact to
        trust, not the number of identities passed in.
        """
        if not identities:
            return self.subscription_count(task_id)

        stamp = int(time.time()) if now is None else now
        cols = ", ".join(SUBSCRIPTION_IDENTITY_COLUMNS)
        insert_cols = f"task_id, {cols}, created_at, last_event_id"
        placeholders = ", ".join(["?"] * (len(SUBSCRIPTION_IDENTITY_COLUMNS) + 3))
        values = [
            (task_id, *[identity.get(c) for c in SUBSCRIPTION_IDENTITY_COLUMNS], stamp, 0)
            for identity in identities
        ]

        conn = self.connect()
        try:
            with conn:  # one transaction; commits on success, rolls back on error
                conn.executemany(
                    f"INSERT OR IGNORE INTO {NOTIFY_SUBS_TABLE} ({insert_cols}) "
                    f"VALUES ({placeholders})",
                    values,
                )
            return conn.execute(
                f"SELECT COUNT(*) FROM {NOTIFY_SUBS_TABLE} WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        finally:
            conn.close()

    def copy_subscriptions(self, from_task: str, to_task: str) -> int:
        """Copy every subscription on one card onto another. Returns the target's count.

        A board operation rather than two calls in a caller, because the pair is
        what has meaning: the child card should reach the same chat thread the
        parent does.
        """
        identities = self.subscriptions_for(from_task)
        if not identities:
            return 0
        return self.add_subscriptions(to_task, identities)

    # ---- introspection -------------------------------------------------------

    def missing_columns(self, table: str, columns: Iterable[str]) -> list[str]:
        """Columns this module expects that the board does not have.

        Exists so a caller can assert its assumptions against a real board and get
        a named list back, rather than an `OperationalError` mid-transaction.
        """
        conn = self.connect()
        try:
            present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            return [c for c in columns if c not in present]
        finally:
            conn.close()
