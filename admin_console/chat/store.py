"""Thread-safe interaction and event storage."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from admin_console.chat.models import (
    Interaction,
    InteractionEvent,
    InteractionStatus,
    TaskProjection,
    ToolCallEvidence,
    utc_now,
)
from admin_console.telemetry import redact_evidence


class InteractionStoreProtocol(Protocol):
    def create(self, interaction: Interaction) -> Interaction: ...

    def get(self, interaction_id: str) -> Interaction | None: ...

    def update(self, interaction_id: str, **changes) -> Interaction: ...

    def transition(
        self,
        interaction_id: str,
        expected: frozenset[InteractionStatus],
        *,
        event: str | None = None,
        event_data: dict | None = None,
        **changes,
    ) -> Interaction | None: ...

    def append_event(
        self,
        interaction_id: str,
        event: str,
        data: dict | None = None,
    ) -> InteractionEvent: ...

    def events_after(
        self,
        interaction_id: str,
        sequence: int = 0,
    ) -> tuple[InteractionEvent, ...]: ...

    def wait_for_change(
        self,
        interaction_id: str,
        *,
        after_sequence: int,
        timeout: float,
    ) -> None: ...

    def recover_incomplete(self) -> int: ...

    def prune(self, *, retention_days: int = 7, maximum: int = 1_000) -> int: ...


class InteractionStore:
    """In-process store used by unit tests and embedded component tests."""

    def __init__(self) -> None:
        self._interactions: dict[str, Interaction] = {}
        self._events: dict[str, list[InteractionEvent]] = {}
        self._condition = threading.Condition(threading.RLock())

    def create(self, interaction: Interaction) -> Interaction:
        with self._condition:
            if interaction.interaction_id in self._interactions:
                raise ValueError("interaction already exists")
            self._interactions[interaction.interaction_id] = interaction
            self._events[interaction.interaction_id] = []
            self._condition.notify_all()
            return interaction

    def get(self, interaction_id: str) -> Interaction | None:
        with self._condition:
            return self._interactions.get(interaction_id)

    def update(self, interaction_id: str, **changes) -> Interaction:
        with self._condition:
            current = self._interactions.get(interaction_id)
            if current is None:
                raise KeyError(interaction_id)
            updated = replace(current, updated_at=utc_now(), **changes)
            self._interactions[interaction_id] = updated
            self._condition.notify_all()
            return updated

    def transition(
        self,
        interaction_id: str,
        expected: frozenset[InteractionStatus],
        *,
        event: str | None = None,
        event_data: dict | None = None,
        **changes,
    ) -> Interaction | None:
        """Apply a state change only when the current status is expected.

        ``event`` is appended in the same critical section as the status
        change: a waiter woken by the new status can never read the event
        stream from before the event that announces it (#1210).
        """
        with self._condition:
            current = self._interactions.get(interaction_id)
            if current is None:
                raise KeyError(interaction_id)
            if current.status not in expected:
                return None
            updated = replace(current, updated_at=utc_now(), **changes)
            self._interactions[interaction_id] = updated
            if event is not None:
                self._append_event_locked(interaction_id, event, event_data)
            self._condition.notify_all()
            return updated

    def _append_event_locked(
        self,
        interaction_id: str,
        event: str,
        data: dict | None,
    ) -> InteractionEvent:
        events = self._events[interaction_id]
        item = InteractionEvent(len(events) + 1, event, utc_now(), data or {})
        events.append(item)
        return item

    def append_event(
        self,
        interaction_id: str,
        event: str,
        data: dict | None = None,
    ) -> InteractionEvent:
        with self._condition:
            if interaction_id not in self._interactions:
                raise KeyError(interaction_id)
            item = self._append_event_locked(interaction_id, event, data)
            self._condition.notify_all()
            return item

    def events_after(
        self,
        interaction_id: str,
        sequence: int = 0,
    ) -> tuple[InteractionEvent, ...]:
        with self._condition:
            if interaction_id not in self._events:
                raise KeyError(interaction_id)
            return tuple(
                event
                for event in self._events[interaction_id]
                if event.sequence > sequence
            )

    def wait_for_change(
        self,
        interaction_id: str,
        *,
        after_sequence: int,
        timeout: float,
    ) -> None:
        with self._condition:
            self._condition.wait_for(
                lambda: len(self._events.get(interaction_id, ())) > after_sequence
                or (
                    self._interactions.get(interaction_id) is not None
                    and self._interactions[interaction_id].terminal
                ),
                timeout=max(0.0, timeout),
            )

    def recover_incomplete(self) -> int:
        return 0

    def prune(self, *, retention_days: int = 7, maximum: int = 1_000) -> int:
        return 0


def interaction_state_path() -> Path:
    override = os.environ.get("KUBE_AGENTS_ADMIN_INTERACTION_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    state_root = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(state_root).expanduser() if state_root else Path.home() / ".local/state"
    return root / "kube-agents" / "admin-portal-interactions.db"


def _encode_interaction(interaction: Interaction) -> str:
    payload = {
        "interaction_id": interaction.interaction_id,
        "agent_id": interaction.agent_id,
        "profile": interaction.profile,
        "session_id": interaction.session_id,
        "input_text": redact_evidence(interaction.input_text),
        "status": interaction.status.value,
        "created_at": interaction.created_at.isoformat(),
        "updated_at": interaction.updated_at.isoformat(),
        "root_run_id": interaction.root_run_id,
        "output": interaction.output,
        "error": interaction.error,
        "diagnostics": list(interaction.diagnostics),
        "approval": interaction.approval,
        "tasks": [
            {
                "task_id": task.task_id,
                "title": task.title,
                "assignee": task.assignee,
                "status": task.status,
                "summary": task.summary,
                "error": task.error,
                "run_count": task.run_count,
            }
            for task in interaction.tasks
        ],
        "tool_calls": [
            {
                "name": tool.name,
                "status": tool.status,
                "source": tool.source,
            }
            for tool in interaction.tool_calls
        ],
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _decode_task_projection(payload: dict) -> TaskProjection:
    """Decode known task fields while tolerating additive stored metadata."""
    return TaskProjection(
        task_id=str(payload["task_id"]),
        title=str(payload["title"]),
        assignee=str(payload["assignee"]),
        status=str(payload["status"]),
        summary=str(payload.get("summary") or ""),
        error=str(payload.get("error") or ""),
        run_count=int(payload.get("run_count") or 0),
    )


def _decode_tool_call(payload: dict) -> ToolCallEvidence:
    """Decode known tool fields while tolerating additive stored metadata."""
    return ToolCallEvidence(
        name=str(payload["name"]),
        status=str(payload["status"]),
        source=str(payload.get("source") or "root_run"),
    )


def _decode_interaction(raw: str) -> Interaction:
    payload = json.loads(raw)
    return Interaction(
        interaction_id=str(payload["interaction_id"]),
        agent_id=str(payload["agent_id"]),
        profile=str(payload["profile"]),
        session_id=str(payload["session_id"]),
        input_text=str(payload["input_text"]),
        status=InteractionStatus(payload["status"]),
        created_at=datetime.fromisoformat(payload["created_at"]),
        updated_at=datetime.fromisoformat(payload["updated_at"]),
        root_run_id=str(payload.get("root_run_id") or ""),
        output=str(payload.get("output") or ""),
        error=str(payload.get("error") or ""),
        diagnostics=tuple(str(item) for item in payload.get("diagnostics", [])),
        approval=payload.get("approval"),
        tasks=tuple(
            _decode_task_projection(task) for task in payload.get("tasks", [])
        ),
        tool_calls=tuple(
            _decode_tool_call(tool) for tool in payload.get("tool_calls", [])
        ),
    )


class SQLiteInteractionStore:
    """Owner-only, single-process durable interaction and event store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._interaction_conditions: dict[str, threading.Condition] = {}
        self._prepare_path()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS interactions (
                    interaction_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS interaction_events (
                    interaction_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (interaction_id, sequence),
                    FOREIGN KEY (interaction_id)
                        REFERENCES interactions(interaction_id) ON DELETE CASCADE
                );
                """
            )
        os.chmod(self.path, 0o600)

    def _condition_for(self, interaction_id: str) -> threading.Condition:
        return self._interaction_conditions.setdefault(
            interaction_id,
            threading.Condition(self._lock),
        )

    def _notify(self, interaction_id: str) -> None:
        self._condition_for(interaction_id).notify_all()

    def _prepare_path(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = self.path.parent.stat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimeError("interaction state directory must be owner-only")
        try:
            database = self.path.lstat()
        except FileNotFoundError:
            return
        if (
            stat.S_ISLNK(database.st_mode)
            or database.st_uid != os.geteuid()
            or stat.S_IMODE(database.st_mode) & 0o077
        ):
            raise RuntimeError("interaction state file must be owner-only and not a symlink")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("PRAGMA secure_delete = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, interaction: Interaction) -> Interaction:
        with self._condition, self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO interactions (interaction_id, payload, updated_at) "
                    "VALUES (?, ?, ?)",
                    (
                        interaction.interaction_id,
                        _encode_interaction(interaction),
                        interaction.updated_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("interaction already exists") from exc
            self._notify(interaction.interaction_id)
        return interaction

    def get(self, interaction_id: str) -> Interaction | None:
        with self._condition, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            return _decode_interaction(row[0]) if row else None

    def update(self, interaction_id: str, **changes) -> Interaction:
        with self._condition, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise KeyError(interaction_id)
            updated = replace(
                _decode_interaction(row[0]),
                updated_at=utc_now(),
                **changes,
            )
            connection.execute(
                "UPDATE interactions SET payload = ?, updated_at = ? "
                "WHERE interaction_id = ?",
                (
                    _encode_interaction(updated),
                    updated.updated_at.isoformat(),
                    interaction_id,
                ),
            )
            self._notify(interaction_id)
            return updated

    def transition(
        self,
        interaction_id: str,
        expected: frozenset[InteractionStatus],
        *,
        event: str | None = None,
        event_data: dict | None = None,
        **changes,
    ) -> Interaction | None:
        """Apply a state change only when the current status is expected.

        ``event`` is written on the same connection, inside the same lock,
        as the status change: a waiter woken by the new status can never
        read the event stream from before the event that announces it
        (#1210). The ``with connection`` context commits both or neither.
        """
        with self._condition, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if row is None:
                raise KeyError(interaction_id)
            current = _decode_interaction(row[0])
            if current.status not in expected:
                return None
            updated = replace(current, updated_at=utc_now(), **changes)
            connection.execute(
                "UPDATE interactions SET payload = ?, updated_at = ? "
                "WHERE interaction_id = ?",
                (
                    _encode_interaction(updated),
                    updated.updated_at.isoformat(),
                    interaction_id,
                ),
            )
            if event is not None:
                self._insert_event(connection, interaction_id, event, event_data)
            self._notify(interaction_id)
            return updated

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        interaction_id: str,
        event: str,
        data: dict | None,
    ) -> InteractionEvent:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM interaction_events "
            "WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        item = InteractionEvent(int(row[0]) + 1, event, utc_now(), data or {})
        connection.execute(
            "INSERT INTO interaction_events "
            "(interaction_id, sequence, event, occurred_at, data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                interaction_id,
                item.sequence,
                item.event,
                item.occurred_at.isoformat(),
                json.dumps(item.data, separators=(",", ":"), sort_keys=True),
            ),
        )
        return item

    def append_event(
        self,
        interaction_id: str,
        event: str,
        data: dict | None = None,
    ) -> InteractionEvent:
        with self._condition, self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(interaction_id)
            item = self._insert_event(connection, interaction_id, event, data)
            self._notify(interaction_id)
            return item

    def events_after(
        self,
        interaction_id: str,
        sequence: int = 0,
    ) -> tuple[InteractionEvent, ...]:
        with self._condition, self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(interaction_id)
            rows = connection.execute(
                "SELECT sequence, event, occurred_at, data "
                "FROM interaction_events WHERE interaction_id = ? AND sequence > ? "
                "ORDER BY sequence",
                (interaction_id, sequence),
            ).fetchall()
            return tuple(
                InteractionEvent(
                    sequence=int(row[0]),
                    event=str(row[1]),
                    occurred_at=datetime.fromisoformat(row[2]),
                    data=json.loads(row[3]),
                )
                for row in rows
            )

    def wait_for_change(
        self,
        interaction_id: str,
        *,
        after_sequence: int,
        timeout: float,
    ) -> None:
        with self._condition:
            condition = self._condition_for(interaction_id)

            def changed() -> bool:
                try:
                    if self.events_after(interaction_id, after_sequence):
                        return True
                except KeyError:
                    return True
                interaction = self.get(interaction_id)
                return interaction is None or interaction.terminal

            condition.wait_for(
                changed,
                timeout=max(0.0, timeout),
            )

    def recover_incomplete(self) -> int:
        recovered = 0
        with self._condition, self._connect() as connection:
            rows = connection.execute("SELECT interaction_id, payload FROM interactions").fetchall()
        for interaction_id, raw in rows:
            interaction = _decode_interaction(raw)
            if interaction.terminal:
                continue
            # transition(event=...) rather than update-then-append: the same
            # atomicity the live paths guarantee — a terminal status is never
            # durable without the event announcing it, even if the process
            # dies again mid-recovery.
            updated = self.transition(
                interaction_id,
                frozenset({interaction.status}),
                event="interaction.recovery_failed",
                event_data={"previousStatus": interaction.status.value},
                status=InteractionStatus.FAILED,
                error="The portal API restarted before this interaction completed.",
                diagnostics=(
                    "Inspect the recorded root run and delegated tasks before retrying; "
                    "the API will not infer success after a restart.",
                ),
                approval=None,
            )
            if updated is not None:
                recovered += 1
        return recovered

    def prune(self, *, retention_days: int = 7, maximum: int = 1_000) -> int:
        retention_days = max(1, retention_days)
        maximum = max(1, maximum)
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self._condition, self._connect() as connection:
            candidates = connection.execute(
                "SELECT interaction_id, payload, updated_at FROM interactions "
                "ORDER BY updated_at DESC"
            ).fetchall()
            terminal_seen = 0
            delete_ids: list[str] = []
            for interaction_id, raw, updated_at in candidates:
                interaction = _decode_interaction(raw)
                if not interaction.terminal:
                    continue
                terminal_seen += 1
                if updated_at < cutoff or terminal_seen > maximum:
                    delete_ids.append(str(interaction_id))
            connection.executemany(
                "DELETE FROM interactions WHERE interaction_id = ?",
                ((interaction_id,) for interaction_id in delete_ids),
            )
            for interaction_id in delete_ids:
                self._notify(interaction_id)
            return len(delete_ids)
