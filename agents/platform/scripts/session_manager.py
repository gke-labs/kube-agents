import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SESSION_KV_DB_PATH = "/var/lib/kube-agents/session/session_kv.db"


class SessionManager:
    """Resolve Hermes session metadata from the session_kv store."""

    SESSION_METADATA_KEYS = (
        "session_id",
        "platform",
        "user_id",
        "user_email",
        "user_email_hash",
        "user_resource",
        "chat_id",
        "thread_id",
        "updated_at",
    )

    ENV_SESSION_KEYS = (
        "HERMES_SESSION_ID",
        "SESSION_ID",
        "X_HERMES_SESSION_ID",
        "HERMES_CURRENT_SESSION_ID",
    )

    ENV_SENDER_KEYS = (
        "HERMES_SENDER_ID",
        "SENDER_ID",
        "X_HERMES_SENDER_ID",
        "HERMES_USER_ID",
    )

    def __init__(
        self,
        db_path: Optional[str] = None,
        timeout: float = 2.0,
    ) -> None:
        self.db_path = db_path or os.getenv("SESSION_KV_DB_PATH", DEFAULT_SESSION_KV_DB_PATH)
        self.timeout = timeout

    @staticmethod
    def sanitize_session_id(value: Optional[str]) -> str:
        s = str(value or "").strip()
        if not s:
            return ""
        # Keep alphanumeric, hyphens, underscores, dots, and colons
        safe = "".join(c for c in s if c.isalnum() or c in ("-", "_", ".", ":"))
        return safe[:128]

    def session_id_from_env(self) -> str:
        for key in self.ENV_SESSION_KEYS:
            val = self.sanitize_session_id(os.environ.get(key))
            if val:
                return val
        return ""

    def metadata_for_session(self, session_id: str) -> Dict[str, Any]:
        sid = self.sanitize_session_id(session_id)
        if not sid:
            return {}
        try:
            path = Path(self.db_path)
            if not path.is_file():
                return {}
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=self.timeout)
            try:
                row = conn.execute(
                    "SELECT metadata FROM session_metadata WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                if not row:
                    return {}
                parsed = json.loads(row[0])
                if isinstance(parsed, dict):
                    return parsed
                return {}
            finally:
                conn.close()
        except Exception:
            return {}

    def current_context(
        self,
        explicit_session_id: str = "",
    ) -> Dict[str, Any]:
        session_id = (
            self.session_id_from_env()
            or self.sanitize_session_id(explicit_session_id)
        )
        metadata = self.metadata_for_session(session_id)
        platform = metadata.get("platform") or ""
        sender_id = metadata.get("user_id") or metadata.get("user_email_hash") or metadata.get("user_email") or ""
        user_id = os.environ.get("HERMES_USER_ID") or os.environ.get("HERMES_SENDER_ID") or sender_id
        if user_id and platform and ":" not in str(user_id):
            user_id = f"{platform}:{user_id}"

        return {
            "session_id": session_id,
            "user_id": user_id,
            "sender_id": os.environ.get("HERMES_SENDER_ID") or sender_id,
            "chat_id": metadata.get("chat_id") or "",
            "thread_id": metadata.get("thread_id") or "",
            "metadata": metadata,
        }

    def delegation_headers(self, context: Dict[str, Any]) -> Dict[str, str]:
        metadata = context.get("metadata", {})
        headers: Dict[str, str] = {}
        if context.get("session_id"):
            headers["X-Hermes-Session-Id"] = str(context["session_id"])
        if context.get("user_id"):
            headers["X-Hermes-User-Id"] = str(context["user_id"])
        if context.get("sender_id"):
            headers["X-Hermes-Sender-Id"] = str(context["sender_id"])
        if metadata.get("user_email_hash"):
            headers["X-Hermes-User-Email-Hash"] = str(metadata["user_email_hash"])
        elif metadata.get("user_email"):
            headers["X-Hermes-User-Email"] = str(metadata["user_email"])
        if context.get("chat_id"):
            headers["X-Hermes-Chat-Id"] = str(context["chat_id"])
        if context.get("thread_id"):
            headers["X-Hermes-Thread-Id"] = str(context["thread_id"])
        return headers

    def filter_session_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: metadata[key]
            for key in self.SESSION_METADATA_KEYS
            if key in metadata and metadata[key] is not None
        }
