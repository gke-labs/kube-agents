#!/usr/bin/env python3
"""Unit tests for PII protection in session_store and session_otel_bridge."""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Stub hermes_plugins.hermes_otel.tracer before imports so bridge.py does not need runtime fallbacks
_fake_tracer_mod = types.ModuleType("hermes_plugins.hermes_otel.tracer")
_fake_tracer_mod.get_tracer = lambda *a, **k: MagicMock()
sys.modules["hermes_plugins"] = types.ModuleType("hermes_plugins")
sys.modules["hermes_plugins.hermes_otel"] = types.ModuleType("hermes_plugins.hermes_otel")
sys.modules["hermes_plugins.hermes_otel.tracer"] = _fake_tracer_mod

# Add defaults package to sys.path matching container layout
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "defaults"))

from plugins.common.redactor import AuditRedactor
from plugins.session_otel_bridge.bridge import OtelSessionBridge
from plugins.session_store.store import (
    SessionMetadata,
    SessionMetadataStore,
)


class TestSessionStorePII(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "session_kv.db")
        self._saved_db = os.environ.get("SESSION_KV_DB_PATH")
        self._saved_salt = os.environ.get("SESSION_KV_SALT")
        os.environ["SESSION_KV_DB_PATH"] = self.db_path
        os.environ["SESSION_KV_SALT"] = "test-salt-secret"
        # Reset SessionMetadataStore connection
        SessionMetadataStore._close_unlocked()

    def tearDown(self):
        SessionMetadataStore._close_unlocked()
        if self._saved_db is None:
            os.environ.pop("SESSION_KV_DB_PATH", None)
        else:
            os.environ["SESSION_KV_DB_PATH"] = self._saved_db
        if self._saved_salt is None:
            os.environ.pop("SESSION_KV_SALT", None)
        else:
            os.environ["SESSION_KV_SALT"] = self._saved_salt
        self.temp_dir.cleanup()

    def test_session_metadata_hashes_email(self):
        email = "user@example.com"
        meta = SessionMetadata(
            session_id="s-1",
            platform="google_chat",
            user_id=email,
            user_email=email,
        )
        data = meta.to_dict()
        self.assertNotIn("user_email", data)
        self.assertIn("user_email_hash", data)
        expected_hash = AuditRedactor.hmac_hash(email)
        self.assertEqual(data["user_email_hash"], expected_hash)
        self.assertEqual(data["user_id"], expected_hash)
        self.assertNotIn(email, str(data))

    def test_session_metadata_store_persists_hash_not_email(self):
        email = "secret@example.com"
        meta = SessionMetadata(
            session_id="s-2",
            platform="google_chat",
            user_id=email,
            user_email=email,
        )
        SessionMetadataStore.write("s-2", meta.to_dict())

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT metadata FROM session_metadata WHERE session_id = 's-2'"
            ).fetchone()

        self.assertIsNotNone(row)
        stored_dict = json.loads(row[0])
        self.assertNotIn(email, row[0])
        self.assertIn("user_email_hash", stored_dict)

    def test_otel_session_bridge_anonymizes_identity(self):
        email = "otel-user@example.com"
        meta = SessionMetadata(
            session_id="s-otel",
            platform="google_chat",
            user_id=email,
            user_email=email,
        )
        SessionMetadataStore.write("s-otel", meta.to_dict())

        bridge = OtelSessionBridge(db_path=Path(self.db_path))
        attrs = bridge._span_attributes_for_session("s-otel")

        self.assertIn("user.id", attrs)
        self.assertIn("hermes.sender.id", attrs)
        self.assertNotIn(email, attrs.values())
        self.assertIn(AuditRedactor.hmac_hash(email), attrs["hermes.sender.id"])


class TestSessionManagerPII(unittest.TestCase):
    def test_delegation_headers_uses_email_hash(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "platform" / "scripts"))
        from session_manager import SessionManager
        sm = SessionManager()
        headers = sm.delegation_headers({"metadata": {"user_email_hash": "hash123"}})
        self.assertEqual(headers.get("X-Hermes-User-Email-Hash"), "hash123")
        self.assertNotIn("X-Hermes-User-Email", headers)


if __name__ == "__main__":
    unittest.main()

