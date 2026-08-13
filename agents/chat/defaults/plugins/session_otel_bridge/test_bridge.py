"""Tests for the session_otel_bridge plugin.

`start_span` is monkey-patched onto the global Hermes tracer, so it runs for
every span the agent opens. Two things therefore matter equally: the attributes
it attaches carry no plaintext identity, and nothing it does can raise.

Hermes is not installed in CI's Python environment, so `hermes_plugins` is
stubbed into `sys.modules` before importing the plugin — the module under test
only needs `get_tracer`.
"""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from inspect import Signature
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class _StubTracer:
    """Stands in for the Hermes OTel tracer, recording what it was handed."""

    def __init__(self):
        self.calls = []

    def start_span(self, name, session_id=None, attributes=None):
        self.calls.append({"name": name, "session_id": session_id, "attributes": attributes})
        return f"span:{name}"


_STUB_TRACER = _StubTracer()


def _install_hermes_stub():
    hermes_plugins = types.ModuleType("hermes_plugins")
    hermes_otel = types.ModuleType("hermes_plugins.hermes_otel")
    tracer_module = types.ModuleType("hermes_plugins.hermes_otel.tracer")
    tracer_module.get_tracer = lambda: _STUB_TRACER
    hermes_otel.tracer = tracer_module
    hermes_plugins.hermes_otel = hermes_otel
    sys.modules.setdefault("hermes_plugins", hermes_plugins)
    sys.modules.setdefault("hermes_plugins.hermes_otel", hermes_otel)
    sys.modules.setdefault("hermes_plugins.hermes_otel.tracer", tracer_module)


_install_hermes_stub()

from common.redactor import SALT_ENV_VAR, AuditRedactor  # noqa: E402

import bridge  # noqa: E402
from bridge import OtelSessionBridge  # noqa: E402

EMAIL = "alice@example.com"


class BridgeTestCase(unittest.TestCase):

    def setUp(self):
        self._previous_salt = os.environ.get(SALT_ENV_VAR)
        os.environ[SALT_ENV_VAR] = "test-salt"
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "session_kv.db"
        self.bridge = OtelSessionBridge(db_path=self.db_path)

    def tearDown(self):
        self._tmp.cleanup()
        if self._previous_salt is None:
            os.environ.pop(SALT_ENV_VAR, None)
        else:
            os.environ[SALT_ENV_VAR] = self._previous_salt

    def write_metadata(self, session_id, metadata):
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS session_metadata ("
                "session_id TEXT PRIMARY KEY, metadata TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                (session_id, json.dumps(metadata)),
            )
            conn.commit()
        finally:
            conn.close()


class TestSpanAttributes(BridgeTestCase):

    def test_a_hashed_row_becomes_span_attributes(self):
        digest = AuditRedactor.hmac_hash(EMAIL)
        self.write_metadata(
            "sess-1",
            {
                "platform": "google_chat",
                "user_id": digest,
                "user_email_hash": digest,
                "chat_id": "spaces/AAA",
                "thread_id": "spaces/AAA/threads/BBB",
            },
        )
        attributes = self.bridge._span_attributes_for_session("sess-1")
        self.assertEqual(attributes["session.id"], "sess-1")
        self.assertEqual(attributes["hermes.sender.id"], digest)
        self.assertEqual(attributes["user.id"], f"google_chat:{digest}")
        self.assertEqual(attributes["chat.id"], "spaces/AAA")
        self.assertEqual(attributes["chat.platform"], "google_chat")

    def test_a_legacy_plaintext_row_is_hashed_on_the_way_out(self):
        # Rows written before pseudonymisation carry the address itself; the
        # span must not.
        self.write_metadata("sess-2", {"platform": "google_chat", "user_email": EMAIL})
        attributes = self.bridge._span_attributes_for_session("sess-2")
        self.assertEqual(attributes["hermes.sender.id"], AuditRedactor.hmac_hash(EMAIL))
        self.assertNotIn(EMAIL, json.dumps(attributes))

    def test_a_legacy_plaintext_user_id_is_hashed_on_the_way_out(self):
        # On Google Chat the pre-migration `user_id` *is* the address. The
        # server-side purge covers these rows but skips any it cannot parse, so
        # the reader must not depend on it.
        self.write_metadata("sess-2b", {"platform": "google_chat", "user_id": EMAIL})
        attributes = self.bridge._span_attributes_for_session("sess-2b")
        self.assertEqual(attributes["hermes.sender.id"], AuditRedactor.hmac_hash(EMAIL))
        self.assertNotIn(EMAIL, json.dumps(attributes))

    def test_the_hash_is_preferred_over_a_leftover_plaintext_field(self):
        self.write_metadata(
            "sess-3",
            {"platform": "google_chat", "user_email_hash": "deadbeef", "user_email": EMAIL},
        )
        attributes = self.bridge._span_attributes_for_session("sess-3")
        self.assertEqual(attributes["hermes.sender.id"], "deadbeef")

    def test_an_already_qualified_user_id_is_not_prefixed_twice(self):
        self.write_metadata("sess-4", {"platform": "slack", "user_id": "slack:U012ABCDEF"})
        attributes = self.bridge._span_attributes_for_session("sess-4")
        self.assertEqual(attributes["user.id"], "slack:U012ABCDEF")

    def test_empty_values_are_dropped(self):
        self.write_metadata("sess-5", {"platform": "slack", "user_id": "U1"})
        attributes = self.bridge._span_attributes_for_session("sess-5")
        self.assertNotIn("chat.id", attributes)
        self.assertNotIn("chat.thread_id", attributes)

    def test_only_declared_attribute_names_are_emitted(self):
        self.write_metadata("sess-6", {"platform": "slack", "user_id": "U1", "extra": "x"})
        attributes = self.bridge._span_attributes_for_session("sess-6")
        self.assertTrue(set(attributes) <= set(OtelSessionBridge.SPAN_ATTRIBUTE_NAMES))


class TestFailureModes(BridgeTestCase):
    """Nothing here may raise: the caller is every span the agent opens."""

    def test_a_missing_database_yields_no_attributes(self):
        self.assertEqual(self.bridge._span_attributes_for_session("sess-1"), {})

    def test_an_unknown_session_yields_no_attributes(self):
        self.write_metadata("sess-1", {"platform": "slack"})
        self.assertEqual(self.bridge._span_attributes_for_session("nope"), {})

    def test_a_corrupt_database_yields_no_attributes(self):
        self.db_path.write_bytes(b"this is not a SQLite file")
        self.assertEqual(self.bridge._metadata_for_session("sess-1"), {})

    def test_unparseable_metadata_yields_no_attributes(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, metadata TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO session_metadata VALUES ('sess-1', 'not json')")
        conn.commit()
        conn.close()
        self.assertEqual(self.bridge._metadata_for_session("sess-1"), {})

    def test_a_json_scalar_is_rejected_rather_than_returned(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, metadata TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO session_metadata VALUES ('sess-1', '\"a string\"')")
        conn.commit()
        conn.close()
        self.assertEqual(self.bridge._metadata_for_session("sess-1"), {})

    def test_a_lookup_failure_degrades_to_the_original_attributes(self):
        def explode(session_id):
            raise RuntimeError("sqlite is on fire")

        self.bridge._span_attributes_for_session = explode
        with self.assertLogs(bridge.logger, level="WARNING") as captured:
            merged = self.bridge._merge_fixed_session_attributes("sess-1", {"kept": "yes"})
        self.assertEqual(merged, {"kept": "yes"})
        self.assertIn("sqlite is on fire", captured.output[0])

    def test_the_session_id_is_sanitised(self):
        self.assertEqual(
            self.bridge._sanitize_session_id("sess-1'; DROP TABLE session_metadata; --"),
            "sess-1DROPTABLEsession_metadata--",
        )
        self.assertEqual(self.bridge._sanitize_session_id(None), "")


class TestTracerPatching(BridgeTestCase):

    def setUp(self):
        super().setUp()
        self.tracer = _STUB_TRACER
        self.tracer.calls.clear()
        if hasattr(self.tracer, OtelSessionBridge.INSTALLED_FLAG):
            delattr(self.tracer, OtelSessionBridge.INSTALLED_FLAG)
        self._original_start_span = _StubTracer.start_span.__get__(self.tracer, _StubTracer)
        self.tracer.start_span = self._original_start_span

    def tearDown(self):
        self.tracer.start_span = self._original_start_span
        if hasattr(self.tracer, OtelSessionBridge.INSTALLED_FLAG):
            delattr(self.tracer, OtelSessionBridge.INSTALLED_FLAG)
        super().tearDown()

    def test_patching_is_idempotent(self):
        self.bridge.patch_tracer()
        patched = self.tracer.start_span
        OtelSessionBridge(db_path=self.db_path).patch_tracer()
        self.assertIs(self.tracer.start_span, patched)

    def test_a_patched_span_carries_the_session_attributes(self):
        self.write_metadata("sess-1", {"platform": "slack", "user_id": "U012ABCDEF"})
        self.bridge.patch_tracer()
        self.tracer.start_span("agent.turn", session_id="sess-1", attributes={"kept": "yes"})
        attributes = self.tracer.calls[-1]["attributes"]
        self.assertEqual(attributes["kept"], "yes")
        self.assertEqual(attributes["hermes.sender.id"], "U012ABCDEF")

    def test_hermes_own_session_id_argument_is_left_alone(self):
        self.bridge.patch_tracer()
        self.tracer.start_span("agent.turn", session_id="sess-1")
        self.assertEqual(self.tracer.calls[-1]["session_id"], "sess-1")

    def test_a_span_without_a_session_still_opens(self):
        self.bridge.patch_tracer()
        self.assertEqual(self.tracer.start_span("agent.turn"), "span:agent.turn")
        self.assertEqual(self.tracer.calls[-1]["attributes"], {})

    def test_an_incompatible_tracer_signature_is_refused_loudly(self):
        # Better to fail at install time than to silently stop attaching
        # identity to every span after a Hermes upgrade.
        signature = Signature.from_callable(lambda name: None)
        with self.assertRaises(RuntimeError) as raised:
            self.bridge._validate_start_span_signature(signature)
        self.assertIn("session_id", str(raised.exception))
        self.assertIn("attributes", str(raised.exception))

    def test_start_span_before_install_is_a_clear_error(self):
        with self.assertRaises(RuntimeError):
            OtelSessionBridge(db_path=self.db_path).start_span("agent.turn")


if __name__ == "__main__":
    unittest.main()
