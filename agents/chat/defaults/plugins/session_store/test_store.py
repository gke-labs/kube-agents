"""Tests for the session_store plugin.

The property under test is that no plaintext identity reaches SQLite: what the
Platform Agent later reads back out of `session_metadata` is exactly what a
compromised database would leak.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.redactor import SALT_ENV_VAR, AuditRedactor  # noqa: E402

import store  # noqa: E402
from store import SessionMetadata, SessionMetadataStore, log_event_to_db  # noqa: E402

EMAIL = "alice@example.com"


def _event(platform="google_chat", user_id=EMAIL, **source_fields):
    fields = {
        "platform": platform,
        "user_id": user_id,
        "user_id_alt": "users/12345",
        "chat_id": "spaces/AAA",
        "thread_id": "spaces/AAA/threads/BBB",
    }
    fields.update(source_fields)
    return SimpleNamespace(source=SimpleNamespace(**fields), text="hello")


class _SessionStore:
    def __init__(self, session_id="sess-1"):
        self.session_id = session_id

    def get_or_create_session(self, source):
        return SimpleNamespace(session_id=self.session_id)


class SaltedTestCase(unittest.TestCase):
    def setUp(self):
        self._previous_salt = os.environ.get(SALT_ENV_VAR)
        os.environ[SALT_ENV_VAR] = "test-salt"

    def tearDown(self):
        if self._previous_salt is None:
            os.environ.pop(SALT_ENV_VAR, None)
        else:
            os.environ[SALT_ENV_VAR] = self._previous_salt


class TestSessionMetadata(SaltedTestCase):

    def test_google_chat_user_id_is_hashed(self):
        metadata = SessionMetadata.from_event(_event(), "sess-1")
        self.assertEqual(metadata.user_id, AuditRedactor.hmac_hash(EMAIL))
        self.assertEqual(metadata.user_email_hash, AuditRedactor.hmac_hash(EMAIL))

    def test_slack_member_id_stays_readable(self):
        metadata = SessionMetadata.from_event(_event(platform="slack", user_id="U012ABCDEF"), "s")
        self.assertEqual(metadata.user_id, "U012ABCDEF")
        self.assertEqual(metadata.user_email_hash, "")

    def test_an_enum_like_platform_is_unwrapped(self):
        source = SimpleNamespace(platform=SimpleNamespace(value="google_chat"), user_id=EMAIL)
        metadata = SessionMetadata.from_event(SimpleNamespace(source=source), "sess-1")
        self.assertEqual(metadata.platform, "google_chat")

    def test_an_event_without_a_source_still_yields_a_row(self):
        metadata = SessionMetadata.from_event(SimpleNamespace(source=None), "sess-1")
        self.assertEqual(metadata.to_dict()["session_id"], "sess-1")

    def test_the_dict_has_no_plaintext_key_at_all(self):
        data = SessionMetadata.from_event(_event(), "sess-1").to_dict()
        self.assertNotIn("user_email", data)
        self.assertNotIn("user_email", SessionMetadata.KEYS)
        self.assertNotIn(EMAIL, json.dumps(data))

    def test_routing_fields_survive(self):
        data = SessionMetadata.from_event(_event(), "sess-1").to_dict()
        self.assertEqual(data["chat_id"], "spaces/AAA")
        self.assertEqual(data["thread_id"], "spaces/AAA/threads/BBB")
        self.assertEqual(data["user_resource"], "users/12345")

    def test_empty_fields_are_dropped(self):
        data = SessionMetadata(session_id="sess-1").to_dict()
        self.assertEqual(set(data), {"session_id", "updated_at"})

    def test_a_supplied_hash_is_not_rehashed(self):
        metadata = SessionMetadata(session_id="s", user_email_hash="deadbeef", user_email=EMAIL)
        self.assertEqual(metadata.user_email_hash, "deadbeef")


class TestWriteThrough(SaltedTestCase):

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self._previous_db = os.environ.get("SESSION_KV_DB_PATH")
        os.environ["SESSION_KV_DB_PATH"] = str(Path(self._tmp.name) / "session_kv.db")
        SessionMetadataStore._close_unlocked()

    def tearDown(self):
        SessionMetadataStore._close_unlocked()
        if self._previous_db is None:
            os.environ.pop("SESSION_KV_DB_PATH", None)
        else:
            os.environ["SESSION_KV_DB_PATH"] = self._previous_db
        self._tmp.cleanup()
        super().tearDown()

    def _stored(self, session_id):
        conn = sqlite3.connect(os.environ["SESSION_KV_DB_PATH"])
        try:
            row = conn.execute(
                "SELECT metadata FROM session_metadata WHERE session_id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def test_the_address_never_reaches_the_database(self):
        log_event_to_db(_event(), gateway=None, session_store=_SessionStore())
        raw = self._stored("sess-1")
        self.assertIsNotNone(raw)
        self.assertNotIn(EMAIL, raw)
        self.assertNotIn("alice", raw)
        self.assertIn(AuditRedactor.hmac_hash(EMAIL), raw)

    def test_the_hook_returns_none_so_dispatch_continues(self):
        self.assertIsNone(log_event_to_db(_event(), None, _SessionStore()))

    def test_a_broken_session_store_does_not_break_dispatch(self):
        class Exploding:
            def get_or_create_session(self, source):
                raise RuntimeError("session store is down")

        with self.assertLogs(store.logger, level="ERROR"):
            self.assertIsNone(log_event_to_db(_event(), None, Exploding()))

    def test_an_unwritable_database_is_logged_not_raised(self):
        os.environ["SESSION_KV_DB_PATH"] = "/proc/kube-agents-does-not-exist/session.db"
        SessionMetadataStore._close_unlocked()
        with self.assertLogs(store.logger, level="ERROR"):
            store.write_session_metadata("sess-1", {"session_id": "sess-1"})

    def test_an_empty_session_id_writes_nothing(self):
        # Not even the database file: the early return happens before the
        # connection is opened.
        store.write_session_metadata("", {"session_id": ""})
        self.assertFalse(Path(os.environ["SESSION_KV_DB_PATH"]).exists())


if __name__ == "__main__":
    unittest.main()
