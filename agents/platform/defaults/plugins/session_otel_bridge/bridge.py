import json
import os
import sqlite3
from inspect import signature
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_plugins.hermes_otel.tracer import get_tracer


class OtelSessionBridge:
    """Attach fixed session metadata to Hermes OTel spans."""

    INSTALLED_FLAG = "_session_otel_bridge_installed"
    SPAN_ATTRIBUTE_NAMES = (
        "session.id",
        "user.id",
        "hermes.sender.id",
        "chat.id",
        "chat.thread_id",
        "chat.platform",
    )

    def __init__(self, db_path: Optional[Path] = None) -> None:
        hermes_home = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))
        self.db_path = db_path or Path(os.environ.get("SESSION_KV_DB_PATH", str(hermes_home / "session_kv.db")))
        self._span_attribute_cache: Dict[str, dict] = {}

    def install(self) -> None:
        tracer = get_tracer()
        if getattr(tracer, self.INSTALLED_FLAG, False):
            return

        original_start_span = tracer.start_span
        start_span_signature = signature(original_start_span)

        def start_span_with_session_attributes(*args: Any, **kwargs: Any) -> Any:
            bound = start_span_signature.bind_partial(*args, **kwargs)
            session_id = self._sanitize_session_id(bound.arguments.get("session_id"))
            attributes = bound.arguments.get("attributes")
            bound.arguments["attributes"] = self._merge_fixed_session_attributes(
                session_id,
                attributes,
            )
            return original_start_span(*bound.args, **bound.kwargs)

        tracer.start_span = start_span_with_session_attributes
        setattr(tracer, self.INSTALLED_FLAG, True)

    def _merge_fixed_session_attributes(self, session_id: str, attributes: Optional[dict]) -> dict:
        attrs = dict(attributes or {})
        session_attrs = self._span_attributes_for_session(session_id)
        if session_attrs:
            attrs.update(session_attrs)
        return attrs

    def _span_attributes_for_session(self, session_id: str) -> dict:
        session_id = self._sanitize_session_id(session_id)
        if session_id in self._span_attribute_cache:
            return dict(self._span_attribute_cache[session_id])

        metadata = self._metadata_for_session(session_id)
        if not metadata:
            return {}

        platform = metadata.get("platform") or ""
        sender_id = metadata.get("user_id") or metadata.get("user_email") or ""
        user_id = sender_id
        if user_id and platform and ":" not in str(user_id):
            user_id = f"{platform}:{user_id}"

        attributes = {
            "session.id": session_id,
            "user.id": user_id,
            "hermes.sender.id": sender_id,
            "chat.id": metadata.get("chat_id") or "",
            "chat.thread_id": metadata.get("thread_id") or "",
            "chat.platform": platform,
        }
        span_attributes = {
            key: value
            for key, value in attributes.items()
            if key in self.SPAN_ATTRIBUTE_NAMES and value is not None and value != ""
        }
        if span_attributes:
            self._span_attribute_cache[session_id] = dict(span_attributes)
        return span_attributes

    def _metadata_for_session(self, session_id: str) -> Dict[str, Any]:
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

    def _sanitize_session_id(self, value: object) -> str:
        return "".join(c for c in str(value or "") if c.isalnum() or c in "-_.").strip()
