"""Tests for the chat_message_audit hook.

This hook sits on `agent:start` / `agent:end` / `agent:step`, so it sees the
user's raw prompt and the agent's raw reply — the two places a credential
pasted into chat is most likely to appear in a log.
"""

import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.redactor import SALT_ENV_VAR, AuditRedactor  # noqa: E402

import handler  # noqa: E402

EMAIL = "alice@example.com"


class HandlerTestCase(unittest.TestCase):

    def setUp(self):
        self._previous_salt = os.environ.get(SALT_ENV_VAR)
        os.environ[SALT_ENV_VAR] = "test-salt"

    def tearDown(self):
        if self._previous_salt is None:
            os.environ.pop(SALT_ENV_VAR, None)
        else:
            os.environ[SALT_ENV_VAR] = self._previous_salt

    def emit(self, event_type, context):
        with self.assertLogs(handler.logger, level="INFO") as captured:
            asyncio.run(handler.handle(event_type, context))
        self.assertEqual(len(captured.records), 1)
        return json.loads(captured.records[0].getMessage())


class TestEventRouting(HandlerTestCase):

    def test_each_event_type_maps_to_its_audit_event(self):
        for event_type, audit_event in (
            ("agent:start", "chat_message_start"),
            ("agent:end", "chat_message_end"),
            ("agent:step", "chat_message_step"),
        ):
            with self.subTest(event_type=event_type):
                record = self.emit(event_type, {"session_id": "sess-1"})
                self.assertEqual(record["audit_event"], audit_event)
                self.assertEqual(record["session_id"], "sess-1")

    def test_an_unknown_event_type_logs_nothing(self):
        with self.assertNoLogs(handler.logger, level="INFO"):
            asyncio.run(handler.handle("agent:something-new", {"session_id": "sess-1"}))

    def test_a_missing_context_does_not_raise(self):
        record = self.emit("agent:start", None)
        self.assertEqual(record["session_id"], "")
        self.assertEqual(record["platform"], "")

    def test_a_context_that_explodes_is_logged_as_an_error_not_raised(self):
        class Hostile(dict):
            def get(self, key, default=None):
                raise RuntimeError("context is broken")

        with self.assertLogs(handler.logger, level="ERROR") as captured:
            # Non-empty: an empty mapping is falsy and never reaches `.get`.
            asyncio.run(handler.handle("agent:start", Hostile(session_id="s")))
        self.assertIn("chat_message_audit", captured.output[0])


class TestRedaction(HandlerTestCase):

    def test_the_google_chat_user_id_is_pseudonymised(self):
        record = self.emit("agent:start", {"platform": "google_chat", "user_id": EMAIL})
        self.assertEqual(record["user_id"], AuditRedactor.hmac_hash(EMAIL))
        self.assertNotIn(EMAIL, json.dumps(record))

    def test_a_slack_member_id_stays_readable(self):
        record = self.emit("agent:start", {"platform": "slack", "user_id": "U012ABCDEF"})
        self.assertEqual(record["user_id"], "U012ABCDEF")

    def test_a_credential_pasted_into_chat_is_redacted(self):
        record = self.emit(
            "agent:start",
            {"session_id": "s", "message": "use ghp_" + "B" * 36 + " to clone"},
        )
        self.assertNotIn("ghp_", record["message"])
        self.assertIn("[REDACTED_SECRET]", record["message"])

    def test_a_credential_echoed_back_by_the_agent_is_redacted(self):
        record = self.emit(
            "agent:end", {"session_id": "s", "response": f"I mailed {EMAIL} about it"}
        )
        self.assertEqual(record["response"], "I mailed [REDACTED_EMAIL] about it")

    def test_long_text_is_truncated_after_redaction(self):
        record = self.emit(
            "agent:start", {"message": "z" * (handler._TEXT_LOG_LIMIT + 100)}
        )
        self.assertTrue(record["message"].endswith("...(truncated)"))
        self.assertEqual(
            len(record["message"]), handler._TEXT_LOG_LIMIT + len("...(truncated)")
        )


class TestOptionalFields(HandlerTestCase):

    def test_absent_fields_are_omitted_rather_than_emitted_empty(self):
        record = self.emit("agent:start", {"session_id": "s"})
        for key in ("message", "response", "iteration", "tool_names"):
            self.assertNotIn(key, record)

    def test_step_fields_are_passed_through(self):
        record = self.emit(
            "agent:step", {"iteration": 3, "tool_names": ["Bash", "Read"]}
        )
        self.assertEqual(record["iteration"], 3)
        self.assertEqual(record["tool_names"], ["Bash", "Read"])

    def test_an_empty_message_is_still_reported(self):
        # Present-but-empty is a different fact from absent, and the audit trail
        # should be able to tell them apart.
        record = self.emit("agent:start", {"message": ""})
        self.assertEqual(record["message"], "")


if __name__ == "__main__":
    unittest.main()
