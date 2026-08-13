"""Tests for the tool_call_audit plugin.

Every assertion here is really the same one: whatever this plugin hands to
`logger.info` is what ends up in Cloud Logging, so the emitted JSON is the
artifact under test — not the arguments it was called with.
"""

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.redactor import SALT_ENV_VAR, AuditRedactor  # noqa: E402

import audit  # noqa: E402

EMAIL = "alice@example.com"


class AuditTestCase(unittest.TestCase):

    def setUp(self):
        self._previous_salt = os.environ.get(SALT_ENV_VAR)
        os.environ[SALT_ENV_VAR] = "test-salt"

    def tearDown(self):
        if self._previous_salt is None:
            os.environ.pop(SALT_ENV_VAR, None)
        else:
            os.environ[SALT_ENV_VAR] = self._previous_salt

    def emit(self, call, *args, **kwargs):
        """Run a hook and return the single JSON record it logged."""
        with self.assertLogs(audit.logger, level="INFO") as captured:
            self.assertIsNone(call(*args, **kwargs))
        self.assertEqual(len(captured.records), 1)
        return json.loads(captured.records[0].getMessage())


class TestSerialize(AuditTestCase):

    def test_a_sensitive_key_is_caught_by_name_before_serialisation(self):
        # The value is ordinary text, so only the key can catch it — which is
        # why redaction runs on the structure rather than on the JSON string.
        serialized = audit._serialize({"clientSecret": "the quick brown fox"})
        self.assertNotIn("quick brown fox", serialized)
        self.assertIn("REDACTED_SECRET", serialized)

    def test_truncation_cannot_reveal_what_redaction_removed(self):
        # Redaction runs on the structure first, so the secret is gone before
        # the cut is made: truncation can only ever drop a marker, never split
        # one open. `sort_keys` puts "note" first, so "password" is dropped.
        payload = {"note": "x" * (audit._PAYLOAD_LOG_LIMIT + 500), "password": "hunter2"}
        serialized = audit._serialize(payload)
        self.assertTrue(serialized.endswith("...(truncated)"))
        self.assertNotIn("hunter2", serialized)

    def test_a_marker_before_the_cut_survives_intact(self):
        payload = {"apiKey": "hunter2", "note": "x" * (audit._PAYLOAD_LOG_LIMIT + 500)}
        serialized = audit._serialize(payload)
        self.assertIn('"apiKey": "[REDACTED_SECRET]"', serialized)
        self.assertTrue(serialized.endswith("...(truncated)"))

    def test_a_long_string_is_truncated(self):
        serialized = audit._serialize("y" * (audit._PAYLOAD_LOG_LIMIT + 1))
        self.assertEqual(len(serialized), audit._PAYLOAD_LOG_LIMIT + len("...(truncated)"))

    def test_an_unserialisable_value_falls_back_to_a_redacted_repr(self):
        # Structural redaction cannot see inside an arbitrary object, so the
        # json.dumps fallback has to redact what str() produces.
        class Opaque:
            def __repr__(self):
                return f"<Opaque owner={EMAIL} token=ghp_{'B' * 36}>"

        serialized = audit._serialize({"obj": Opaque()})
        self.assertNotIn(EMAIL, serialized)
        self.assertNotIn("ghp_", serialized)

    def test_output_is_valid_json(self):
        self.assertEqual(json.loads(audit._serialize({"a": 1})), {"a": 1})


class TestToolCallHooks(AuditTestCase):

    def test_pre_tool_call_redacts_args(self):
        record = self.emit(
            audit.log_pre_tool_call,
            tool_name="Bash",
            args={"command": "curl -H 'Authorization: Bearer abcdefghij0123456789'"},
            task_id="t-1",
        )
        self.assertEqual(record["audit_event"], "tool_call_start")
        self.assertEqual(record["tool_name"], "Bash")
        self.assertEqual(record["task_id"], "t-1")
        self.assertNotIn("abcdefghij", record["args"])

    def test_post_tool_call_redacts_the_result(self):
        record = self.emit(
            audit.log_post_tool_call,
            tool_name="kubectl",
            result={"stdout": f"owner {EMAIL}"},
            duration_ms=12.5,
            task_id="t-1",
        )
        self.assertEqual(record["audit_event"], "tool_call_end")
        self.assertEqual(record["duration_ms"], 12.5)
        self.assertNotIn(EMAIL, record["result"])

    def test_approval_hooks_redact_the_command(self):
        for call, event in (
            (audit.log_pre_approval_request, "approval_request"),
            (audit.log_post_approval_response, "approval_response"),
        ):
            with self.subTest(event=event):
                record = self.emit(
                    call,
                    command="gcloud auth print-access-token ya29." + "A" * 40,
                    description="mint a token",
                    pattern_key="gcloud:auth",
                    surface="chat",
                )
                self.assertEqual(record["audit_event"], event)
                self.assertNotIn("ya29.", record["command"])
                self.assertEqual(record["description"], "mint a token")

    def test_unknown_keyword_arguments_are_tolerated(self):
        # Hermes may pass hook arguments this plugin does not know about; a
        # TypeError here would surface as a failed tool call.
        self.emit(audit.log_pre_tool_call, tool_name="Bash", something_new=object())


class TestGatewayDispatch(AuditTestCase):

    def _event(self, platform="google_chat", user_id=EMAIL, text="hello"):
        source = SimpleNamespace(platform=platform, user_id=user_id)
        return SimpleNamespace(source=source, text=text)

    class _Sessions:
        def get_or_create_session(self, source):
            return SimpleNamespace(session_id="sess-1")

    def test_the_address_is_pseudonymised(self):
        record = self.emit(
            audit.log_pre_gateway_dispatch, self._event(), None, self._Sessions()
        )
        self.assertEqual(record["audit_event"], "gateway_dispatch")
        self.assertEqual(record["session_id"], "sess-1")
        self.assertEqual(record["user_id"], AuditRedactor.hmac_hash(EMAIL))
        self.assertNotIn(EMAIL, json.dumps(record))

    def test_a_slack_member_id_stays_readable(self):
        record = self.emit(
            audit.log_pre_gateway_dispatch,
            self._event(platform="slack", user_id="U012ABCDEF"),
            None,
            self._Sessions(),
        )
        self.assertEqual(record["user_id"], "U012ABCDEF")

    def test_message_text_is_redacted(self):
        record = self.emit(
            audit.log_pre_gateway_dispatch,
            self._event(text=f"forward this to {EMAIL}"),
            None,
            self._Sessions(),
        )
        self.assertIn("[REDACTED_EMAIL]", record["text"])

    def test_a_broken_session_store_still_emits_the_record(self):
        class Exploding:
            def get_or_create_session(self, source):
                raise RuntimeError("down")

        record = self.emit(audit.log_pre_gateway_dispatch, self._event(), None, Exploding())
        self.assertEqual(record["session_id"], "")
        self.assertEqual(record["user_id"], AuditRedactor.hmac_hash(EMAIL))

    def test_an_event_without_a_source_does_not_raise(self):
        record = self.emit(
            audit.log_pre_gateway_dispatch, SimpleNamespace(source=None, text=""), None, None
        )
        self.assertEqual(record["platform"], "")
        self.assertEqual(record["user_id"], "")


if __name__ == "__main__":
    unittest.main()
