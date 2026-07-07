import json
import os
import sqlite3
from inspect import Signature, signature
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from hermes_plugins.hermes_otel.tracer import get_tracer

DEFAULT_SESSION_KV_DB_PATH = "/var/lib/kube-agents/session/session_kv.db"


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
        self.db_path = db_path or Path(os.environ.get("SESSION_KV_DB_PATH", DEFAULT_SESSION_KV_DB_PATH))
        self._original_start_span: Optional[Callable[..., Any]] = None
        self._start_span_signature: Optional[Signature] = None

    def patch_tracer(self) -> None:
        tracer = get_tracer()
        if getattr(tracer, self.INSTALLED_FLAG, False):
            return

        self._original_start_span = tracer.start_span
        self._start_span_signature = signature(tracer.start_span)
        self._validate_start_span_signature(self._start_span_signature)
        # Hermes does not currently expose a span-attribute provider hook. Patch
        # the Hermes OTel tracer method once so every future span gets fixed
        # session metadata while preserving Hermes' own session_id handling.
        tracer.start_span = self.start_span
        setattr(tracer, self.INSTALLED_FLAG, True)

    def start_span(self, *args: Any, **kwargs: Any) -> Any:
        if self._original_start_span is None or self._start_span_signature is None:
            raise RuntimeError("session OTel bridge is not installed")

        bound = self._start_span_signature.bind_partial(*args, **kwargs)
        session_id = self._sanitize_session_id(bound.arguments.get("session_id"))
        attributes = bound.arguments.get("attributes")
        bound.arguments["attributes"] = self._merge_fixed_session_attributes(
            session_id,
            attributes,
        )
        return self._original_start_span(*bound.args, **bound.kwargs)

    def _validate_start_span_signature(self, start_span_signature: Signature) -> None:
        parameters = start_span_signature.parameters
        required_parameters = ("session_id", "attributes")
        missing = [name for name in required_parameters if name not in parameters]
        if missing:
            raise RuntimeError(
                "Hermes OTel start_span signature is missing required parameter(s): "
                + ", ".join(missing)
            )

    def _merge_fixed_session_attributes(self, session_id: str, attributes: Optional[dict]) -> dict:
        attrs = dict(attributes or {})
        session_attrs = self._span_attributes_for_session(session_id)
        if session_attrs:
            attrs.update(session_attrs)
        return attrs

    def _span_attributes_for_session(self, session_id: str) -> dict:
        session_id = self._sanitize_session_id(session_id)
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
        return {
            key: value
            for key, value in attributes.items()
            if key in self.SPAN_ATTRIBUTE_NAMES and value is not None and value != ""
        }

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
