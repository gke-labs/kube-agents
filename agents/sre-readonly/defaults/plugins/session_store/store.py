import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes.plugin.session_store")

DEFAULT_SESSION_KV_DB_PATH = "/var/lib/kube-agents/session/session_kv.db"
DEFAULT_RETENTION_DAYS = 7
CREATE_SESSION_METADATA_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS session_metadata (
    session_id TEXT PRIMARY KEY,
    metadata TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


class SessionMetadata:
    """Fixed metadata retained for a Hermes session."""

    KEYS = (
        "session_id",
        "platform",
        "user_id",
        "user_email",
        "user_resource",
        "chat_id",
        "thread_id",
        "updated_at",
    )

    def __init__(
        self,
        session_id: str,
        platform: str = "",
        user_id: str = "",
        user_email: str = "",
        user_resource: str = "",
        chat_id: str = "",
        thread_id: str = "",
        updated_at: str = "",
    ) -> None:
        self.session_id = session_id
        self.platform = platform
        self.user_id = user_id
        self.user_email = user_email
        self.user_resource = user_resource
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.updated_at = updated_at or datetime.now(timezone.utc).isoformat()

    @classmethod
    def from_event(cls, event: Any, session_id: str) -> "SessionMetadata":
        source = getattr(event, "source", None)
        if source is None:
            return cls(session_id=session_id)

        platform = _platform_value(source)
        user_id = getattr(source, "user_id", None) or ""
        user_resource = getattr(source, "user_id_alt", None) or ""

        return cls(
            session_id=session_id,
            platform=platform,
            user_id=user_id,
            user_email=user_id if platform == "google_chat" else "",
            user_resource=user_resource,
            chat_id=getattr(source, "chat_id", None) or "",
            thread_id=getattr(source, "thread_id", None) or "",
        )

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "session_id": self.session_id,
            "platform": self.platform,
            "user_id": self.user_id,
            "user_email": self.user_email,
            "user_resource": self.user_resource,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "updated_at": self.updated_at,
        }
        return {
            key: value
            for key, value in data.items()
            if key in self.KEYS and value is not None and value != ""
        }


def _session_kv_db_path() -> str:
    return os.getenv("SESSION_KV_DB_PATH", DEFAULT_SESSION_KV_DB_PATH)


def _retention_days() -> int:
    raw = os.getenv("SESSION_KV_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def _platform_value(source: Any) -> str:
    platform = getattr(source, "platform", "") or ""
    return getattr(platform, "value", None) or str(platform)


class SessionMetadataStore:
    """Thread-safe lazy SQLite connection for session metadata writes."""

    _conn: Optional[sqlite3.Connection] = None
    _db_path = ""
    _lock = threading.RLock()

    @classmethod
    def get_db_connection(cls) -> sqlite3.Connection:
        db_path = _session_kv_db_path()
        with cls._lock:
            if cls._conn is None or cls._db_path != db_path:
                cls._close_unlocked()
                cls._conn = cls._open_connection(db_path)
                cls._db_path = db_path
            return cls._conn

    @classmethod
    def write(cls, session_id: str, metadata: Dict[str, Any]) -> None:
        payload = json.dumps(metadata, sort_keys=True)
        for attempt in range(2):
            with cls._lock:
                conn = cls.get_db_connection()
                try:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO session_metadata
                            (session_id, metadata, updated_at)
                        VALUES (?, ?, CURRENT_TIMESTAMP)
                        """,
                        (session_id, payload),
                    )
                    conn.execute(
                        "DELETE FROM session_metadata WHERE updated_at < datetime('now', ?)",
                        (f"-{_retention_days()} days",),
                    )
                    conn.commit()
                    return
                except sqlite3.Error:
                    cls._close_unlocked()
                    if attempt == 1:
                        raise

    @classmethod
    def _open_connection(cls, db_path: str) -> sqlite3.Connection:
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        conn.execute(CREATE_SESSION_METADATA_TABLE_SQL)
        conn.commit()
        return conn

    @classmethod
    def _close_unlocked(cls) -> None:
        if cls._conn is not None:
            try:
                cls._conn.close()
            except sqlite3.Error:
                pass
            finally:
                cls._conn = None
                cls._db_path = ""


def write_session_metadata(session_id: str, metadata: Dict[str, Any]) -> None:
    if not session_id:
        return

    try:
        SessionMetadataStore.write(session_id, metadata)
    except Exception as exc:
        logger.error(
            "Failed to write session metadata for session %s: %s",
            session_id,
            exc,
            exc_info=True,
        )


def log_event_to_db(
    event: Any,
    gateway: Any,
    session_store: Any,
    **kwargs: Any,
) -> Optional[Dict[str, str]]:
    """Persist session metadata before Hermes dispatches a gateway message."""
    try:
        source = getattr(event, "source", None)
        if source is None:
            return None

        session_entry = session_store.get_or_create_session(source)
        session_id = getattr(session_entry, "session_id", "") or ""
        metadata = SessionMetadata.from_event(event, session_id)
        write_session_metadata(session_id, metadata.to_dict())
    except Exception as exc:
        logger.error(
            "Error in session_store pre_gateway_dispatch hook: %s",
            exc,
            exc_info=True,
        )

    return None
