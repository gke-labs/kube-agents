"""Tests for the tool_call_audit plugin.

Every assertion here is really the same one: whatever this plugin appends to
the profile's audit file is what ends up in Cloud Logging, so the written JSON
line is the artifact under test — not the arguments it was called with.
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
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import audit_sink  # noqa: E402
from common.redactor import SALT_ENV_VAR, AuditRedactor  # noqa: E402

import audit  # noqa: E402

EMAIL = "alice@example.com"


class AuditTestCase(unittest.TestCase):

    def setUp(self):
        self._previous_salt = os.environ.get(SALT_ENV_VAR)
        os.environ[SALT_ENV_VAR] = "test-salt"
        # A profile home of its own per test, with no logs/ directory yet: the
        # plugin has to bring it into being on the first record.
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

    def emit(self, call, *args, **kwargs):
        """Run a hook and return the single JSON record it appended to the audit file.

        Nothing may reach the Hermes logger on the way: the record is not in
        agent.log any more, and an error there would mean the write failed.
        """
        before = len(self.lines())
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), self.assertNoLogs(audit.logger, level="INFO"):
            self.assertIsNone(call(*args, **kwargs))
        self.assertEqual(printed.getvalue(), "", "the stdout fallback fired on a writable file")
        lines = self.lines()
        self.assertEqual(len(lines), before + 1)
        return json.loads(lines[-1])


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

    def test_tool_call_records_carry_the_session_hermes_names(self):
        # Hermes passes session_id to both tool hooks beside task_id. The file
        # has no Hermes line prefix to read a `[session]` tag from, so the
        # record is where a reader (the Admin Console's cron attribution) gets
        # it now; a call outside any session carries an empty one.
        start = self.emit(
            audit.log_pre_tool_call, tool_name="Bash", args={}, task_id="t-1",
            session_id="cron_capacity_20260728_190038",
        )
        self.assertEqual(start["session_id"], "cron_capacity_20260728_190038")
        end = self.emit(audit.log_post_tool_call, tool_name="Bash", result="ok", session_id="s-2")
        self.assertEqual(end["session_id"], "s-2")
        bare = self.emit(audit.log_pre_tool_call, tool_name="Bash", args={})
        self.assertEqual(bare["session_id"], "")

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

    def test_a_dispatch_on_the_gateway_loop_is_written_off_it(self):
        # Hermes calls pre_gateway_dispatch synchronously on the gateway's event
        # loop; the sink has to see the loop and write on its own thread.
        loop_thread = threading.current_thread()
        writer_threads = []
        real_append = audit_sink.append_line

        def record_thread(line, path=None):
            writer_threads.append(threading.current_thread())
            return real_append(line, path)

        async def dispatch():
            audit.log_pre_gateway_dispatch(self._event(), None, self._Sessions())

        with mock.patch.object(audit_sink, "append_line", side_effect=record_thread):
            asyncio.run(dispatch())
            audit_sink.flush(timeout=5)
        self.assertEqual(len(writer_threads), 1)
        self.assertIsNot(writer_threads[0], loop_thread)
        self.assertEqual(json.loads(self.lines()[0])["audit_event"], "gateway_dispatch")


class TestEnvelope(AuditTestCase):
    """Every record is self-describing: the schema's kind, severity and time, plus
    the status and tool the structured schema names, beside the keys the Admin
    Console has always read."""

    def test_a_tool_call_carries_the_envelope_and_the_schema_fields(self):
        start = self.emit(audit.log_pre_tool_call, tool_name="Bash", args={"command": "ls"}, task_id="t-1")
        self.assertEqual(start["event_type"], "tool_call_start")
        self.assertEqual(start["audit_event"], start["event_type"])
        self.assertEqual(start["severity"], "INFO")
        self.assertRegex(start["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertEqual((start["tool"], start["status"]), ("Bash", "started"))

        end = self.emit(audit.log_post_tool_call, tool_name="Bash", result="ok", duration_ms=7, task_id="t-1")
        self.assertEqual((end["event_type"], end["tool"], end["status"], end["duration_ms"]), ("tool_call_end", "Bash", "completed", 7))

    def test_every_record_is_one_line_of_json(self):
        calls = (
            (audit.log_pre_tool_call, {"tool_name": "Bash", "args": {"command": "echo\nhi"}}),
            (audit.log_post_tool_call, {"tool_name": "Bash", "result": "a\nb"}),
            (audit.log_pre_approval_request, {"command": "rm -rf /\n", "description": "d"}),
            (audit.log_post_approval_response, {"command": "x", "choice": "deny"}),
        )
        for call, kwargs in calls:
            call(**kwargs)
        raw = self.audit_file.read_text(encoding="utf-8")
        self.assertEqual(raw.count("\n"), len(calls), "one newline-terminated line per record")
        for line in raw.splitlines():
            self.assertIsInstance(json.loads(line), dict)

    def test_approvals_and_dispatch_carry_a_status_or_a_principal(self):
        request = self.emit(audit.log_pre_approval_request, command="x", description="d")
        self.assertEqual(request["status"], "requested")
        response = self.emit(audit.log_post_approval_response, command="x", choice="allow")
        self.assertEqual(response["status"], "answered")
        source = SimpleNamespace(platform="google_chat", user_id=EMAIL)
        event = SimpleNamespace(source=source, text="hello")
        dispatch = self.emit(audit.log_pre_gateway_dispatch, event=event)
        self.assertEqual(dispatch["principal"], AuditRedactor.hmac_hash(EMAIL))
        self.assertEqual(dispatch["principal"], dispatch["user_id"])


class TestAuditFile(AuditTestCase):
    """The record goes to the profile's own file, not through Hermes' logger."""

    def test_the_file_is_under_the_profile_home_and_its_directory_is_created(self):
        self.assertFalse(self.audit_file.parent.exists())
        self.emit(audit.log_pre_tool_call, tool_name="Bash", args={})
        self.assertEqual(self.audit_file, self.home / "logs" / "audit.jsonl")
        self.assertTrue(self.audit_file.is_file())

    def test_the_record_goes_to_stdout_when_the_file_cannot_take_it(self):
        # logs/ is a file, so the audit file cannot be opened or created. The
        # record is not dropped: it is printed to this process's stdout as the
        # same JSON line (the container log, from the gateway), and the ERROR
        # that goes to agent.log names the path and nothing of the record.
        self.audit_file.parent.write_text("not a directory")
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), self.assertLogs(audit.logger, level="ERROR") as captured:
            audit.log_pre_tool_call(tool_name="Bash", args={"command": "ls"}, task_id="t-1")
        record = json.loads(printed.getvalue())
        self.assertEqual((record["audit_event"], record["tool"]), ("tool_call_start", "Bash"))
        notice = captured.output[0]
        self.assertIn(str(self.audit_file), notice)
        self.assertNotIn("audit_event", notice)
        self.assertNotIn("Bash", notice)

    def test_a_record_the_envelope_refuses_is_not_written(self):
        with self.assertRaises(ValueError):
            audit._emit("tool_call_start", {"severity": "DEBUG"})
        self.assertFalse(self.audit_file.exists())

    def test_hermes_redaction_runs_over_the_line_after_the_plugins_own(self):
        # A GitLab token: a shape AuditRedactor does not know and Hermes'
        # redactor does. Inside Hermes the line passes through the latter too.
        token = "glpat-ABCDEFGHIJKLMNOPQRST"
        redact = types.ModuleType("agent.redact")
        redact.redact_sensitive_text = lambda text, **_: text.replace(token, "glpat-...QRST")
        with mock.patch.dict(sys.modules, {"agent": types.ModuleType("agent"), "agent.redact": redact}):
            record = self.emit(audit.log_pre_tool_call, tool_name="Bash", args={"command": f"export GL={token}"})
        self.assertNotIn(token, record["args"])
        self.assertIn("glpat-...QRST", record["args"])


if __name__ == "__main__":
    unittest.main()
