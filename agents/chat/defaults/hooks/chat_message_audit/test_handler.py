"""Tests for the chat_message_audit hook.

This hook sits on `agent:start` / `agent:end` / `agent:step`, so it sees the
user's raw prompt and the agent's raw reply — the two places a credential
pasted into chat is most likely to appear in a log. What it appends to the
profile's audit file is what ends up in Cloud Logging, so that line is the
artifact under test.
"""

import asyncio
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import audit_sink  # noqa: E402
from common.redactor import SALT_ENV_VAR, AuditRedactor  # noqa: E402

import handler  # noqa: E402

EMAIL = "alice@example.com"


class HandlerTestCase(unittest.TestCase):

    def setUp(self):
        self._previous_salt = os.environ.get(SALT_ENV_VAR)
        os.environ[SALT_ENV_VAR] = "test-salt"
        # A profile home of its own per test, with no logs/ directory yet: the
        # hook has to bring it into being on the first record.
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        env = mock.patch.dict(os.environ, {audit_sink.HERMES_HOME_ENV: str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.audit_file = self.home / "logs" / "audit.jsonl"

    def tearDown(self):
        if self._previous_salt is None:
            os.environ.pop(SALT_ENV_VAR, None)
        else:
            os.environ[SALT_ENV_VAR] = self._previous_salt

    def lines(self):
        if not self.audit_file.exists():
            return []
        return self.audit_file.read_text(encoding="utf-8").splitlines()

    def emit(self, event_type, context):
        """Run the hook and return the single record it appended to the audit file.

        Nothing may reach the Hermes logger on the way: the record is not in
        agent.log any more, and an error there would mean the write failed.
        """
        before = len(self.lines())
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), self.assertNoLogs(handler.logger, level="INFO"):
            asyncio.run(handler.handle(event_type, context))
            # The hook runs on a loop, so the sink wrote on its writer thread.
            audit_sink.flush(timeout=5)
        self.assertEqual(printed.getvalue(), "", "the stdout fallback fired on a writable file")
        lines = self.lines()
        self.assertEqual(len(lines), before + 1)
        return json.loads(lines[-1])


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

    def test_an_unknown_event_type_writes_nothing(self):
        with self.assertNoLogs(handler.logger, level="INFO"):
            asyncio.run(handler.handle("agent:something-new", {"session_id": "sess-1"}))
            audit_sink.flush(timeout=5)
        self.assertFalse(self.audit_file.exists())

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
        self.assertFalse(self.audit_file.exists(), "a refused record is not written")


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


class TestEnvelope(HandlerTestCase):
    """Every turn record is self-describing under the structured audit schema."""

    def test_the_record_carries_the_envelope_and_the_principal(self):
        record = self.emit("agent:start", {"platform": "google_chat", "user_id": EMAIL, "session_id": "sess-1", "message": "hi"})
        self.assertEqual(record["event_type"], "chat_message_start")
        self.assertEqual(record["audit_event"], record["event_type"])
        self.assertEqual(record["severity"], "INFO")
        self.assertRegex(record["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertEqual(record["principal"], AuditRedactor.hmac_hash(EMAIL))
        self.assertEqual(record["principal"], record["user_id"])

    def test_every_record_is_one_line_of_json(self):
        asyncio.run(handler.handle("agent:end", {"response": "line one\nline two", "session_id": "s"}))
        asyncio.run(handler.handle("agent:start", {"message": "a\nb", "session_id": "s"}))
        audit_sink.flush(timeout=5)
        raw = self.audit_file.read_text(encoding="utf-8")
        self.assertEqual(raw.count("\n"), 2, "one newline-terminated line per record")
        first, second = raw.splitlines()
        self.assertEqual(json.loads(first)["response"], "line one\nline two")
        self.assertEqual(json.loads(second)["message"], "a\nb")


class TestAuditFile(HandlerTestCase):
    """The record goes to the profile's own file, not through Hermes' logger."""

    def test_the_file_is_under_the_profile_home_and_its_directory_is_created(self):
        self.assertFalse(self.audit_file.parent.exists())
        self.emit("agent:start", {"session_id": "s"})
        self.assertEqual(self.audit_file, self.home / "logs" / "audit.jsonl")
        self.assertTrue(self.audit_file.is_file())

    def test_the_record_goes_to_stdout_when_the_file_cannot_take_it(self):
        # logs/ is a file, so the audit file cannot be opened or created. The
        # record is not dropped: it is printed to this process's stdout as the
        # same JSON line (the container log, from the gateway), and the ERROR
        # that goes to agent.log names the path and nothing of the record.
        self.audit_file.parent.write_text("not a directory")
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), self.assertLogs(handler.logger, level="ERROR") as captured:
            asyncio.run(handler.handle("agent:end", {"session_id": "s", "response": "done"}))
            audit_sink.flush(timeout=5)
        record = json.loads(printed.getvalue())
        self.assertEqual((record["audit_event"], record["response"]), ("chat_message_end", "done"))
        notice = captured.output[0]
        self.assertIn(str(self.audit_file), notice)
        self.assertNotIn("audit_event", notice)
        self.assertNotIn("done", notice)

    def test_the_write_leaves_the_event_loop(self):
        # The hook runs on the gateway's loop; the file write must not.
        loop_thread = threading.current_thread()
        writer_threads = []
        real_append = audit_sink.append_line

        def record_thread(line, path=None):
            writer_threads.append(threading.current_thread())
            return real_append(line, path)

        with mock.patch.object(audit_sink, "append_line", side_effect=record_thread):
            asyncio.run(handler.handle("agent:start", {"session_id": "s"}))
            audit_sink.flush(timeout=5)
        self.assertEqual(len(writer_threads), 1)
        self.assertIsNot(writer_threads[0], loop_thread)
        self.assertEqual(len(self.lines()), 1)


if __name__ == "__main__":
    unittest.main()
