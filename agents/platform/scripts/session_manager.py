import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_SESSION_KV_DB_PATH = "/var/lib/kube-agents/session/session_kv.db"


class SessionManager:
    """Resolve Hermes session metadata from the session_kv store."""

    SESSION_METADATA_KEYS = (
        "session_id",
        "platform",
        "user_id",
        "user_email",
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

    def __init__(
        self,
        hermes_home: Optional[Path] = None,
        db_path: Optional[Path] = None,
    ) -> None:
        self.hermes_home = hermes_home or Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
        self.db_path = db_path or Path(os.environ.get("SESSION_KV_DB_PATH", DEFAULT_SESSION_KV_DB_PATH))

    def sanitize_session_id(self, value: object) -> str:
        return "".join(c for c in str(value or "") if c.isalnum() or c in "-_.").strip()

    def session_id_from_env(self) -> str:
        for key in self.ENV_SESSION_KEYS:
            session_id = self.sanitize_session_id(os.environ.get(key))
            if session_id:
                return session_id
        return ""

    def metadata_for_session(self, session_id: str) -> Dict[str, Any]:
        session_id = self.sanitize_session_id(session_id)
        if not session_id or not self.db_path.exists():
            return {}

        conn = None
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=2.0)
            row = conn.execute(
                "SELECT metadata FROM session_metadata WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        except Exception:
            return {}
        finally:
            if conn is not None:
                conn.close()

        if not row:
            return {}

        try:
            metadata = json.loads(row[0])
            return metadata if isinstance(metadata, dict) else {}
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
        sender_id = metadata.get("user_id") or metadata.get("user_email") or ""
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

    @staticmethod
    def canonicalize_headers(headers: Dict[str, str]) -> str:
        normalized = {}
        for key, value in headers.items():
            lower_key = str(key).lower()
            if lower_key.startswith("x-hermes-") and lower_key not in (
                "x-hermes-signature",
                "x-hermes-timestamp",
            ):
                normalized[lower_key] = str(value).strip()
        sorted_keys = sorted(normalized.keys())
        canonical_lines = [
            f"{len(key)}:{key}:{len(normalized[key])}:{normalized[key]}\n"
            for key in sorted_keys
        ]
        return "".join(canonical_lines)

    def _base_delegation_headers(self, context: Dict[str, Any]) -> Dict[str, str]:
        metadata = context.get("metadata", {})
        headers: Dict[str, str] = {}
        if context.get("session_id"):
            headers["X-Hermes-Session-Id"] = str(context["session_id"])
        if context.get("user_id"):
            headers["X-Hermes-User-Id"] = str(context["user_id"])
        if context.get("sender_id"):
            headers["X-Hermes-Sender-Id"] = str(context["sender_id"])
        if metadata.get("user_email"):
            headers["X-Hermes-User-Email"] = str(metadata["user_email"])
        if context.get("chat_id"):
            headers["X-Hermes-Chat-Id"] = str(context["chat_id"])
        if context.get("thread_id"):
            headers["X-Hermes-Thread-Id"] = str(context["thread_id"])
        return headers

    def _resolve_signing_key(self, api_key: str) -> str:
        env_key = os.environ.get("HERMES_DELEGATION_SIGNING_KEY", "").strip()
        if env_key:
            return env_key
        # Derive a distinct signing secret so the raw API key is never used directly as the HMAC key
        return hmac.new(
            api_key.strip().encode("utf-8"),
            b"hermes-delegation-signing-key",
            hashlib.sha256,
        ).hexdigest()

    def signed_delegation_headers(
        self,
        context: Dict[str, Any],
        api_key: str,
        body_digest: str = "",
        target: str = "",
    ) -> Dict[str, str]:
        if not api_key or not str(api_key).strip():
            raise ValueError("API key is required to sign delegation headers.")
        headers = self._base_delegation_headers(context)
        canonical = self.canonicalize_headers(headers)
        header_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        timestamp = str(int(time.time()))
        session_id = str(
            headers.get("X-Hermes-Session-Id")
            or context.get("session_id")
            or ""
        ).strip()
        signing_payload = (
            f"{timestamp}:{session_id}:{target}:{body_digest}:{header_digest}"
        )
        signing_key = self._resolve_signing_key(api_key)
        signature = hmac.new(
            signing_key.encode("utf-8"),
            signing_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["X-Hermes-Signature"] = f"sha256={signature}"
        headers["X-Hermes-Timestamp"] = timestamp
        return headers

    @staticmethod
    def _get_header_ci(headers: Dict[str, str], key: str) -> Optional[str]:
        target = key.lower()
        for k, v in headers.items():
            if str(k).lower() == target:
                return str(v)
        return None

    def verify_delegation_headers(
        self,
        headers: Dict[str, str],
        api_keys: List[str],
        max_skew_seconds: int = 300,
        body_digest: str = "",
        target: str = "",
    ) -> bool:
        sig_header = self._get_header_ci(headers, "X-Hermes-Signature")
        ts_header = self._get_header_ci(headers, "X-Hermes-Timestamp")
        if not sig_header or not ts_header:
            return False

        if not str(sig_header).startswith("sha256="):
            return False
        provided_sig = str(sig_header).removeprefix("sha256=")

        try:
            ts_int = int(str(ts_header).strip())
        except (ValueError, TypeError):
            return False

        if abs(time.time() - ts_int) > max_skew_seconds:
            return False

        valid_keys = [str(k).strip() for k in api_keys if k and str(k).strip()]
        if not valid_keys:
            return False

        canonical = self.canonicalize_headers(headers)
        header_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        session_id = str(
            self._get_header_ci(headers, "X-Hermes-Session-Id") or ""
        ).strip()
        signing_payload = (
            f"{str(ts_header).strip()}:{session_id}:{target}:{body_digest}:{header_digest}"
        )

        for api_key in valid_keys:
            signing_key = self._resolve_signing_key(api_key)
            expected_sig = hmac.new(
                signing_key.encode("utf-8"),
                signing_payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            if hmac.compare_digest(provided_sig, expected_sig):
                return True

        return False

    def filter_session_metadata(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: metadata[key]
            for key in self.SESSION_METADATA_KEYS
            if key in metadata and metadata[key] is not None
        }
