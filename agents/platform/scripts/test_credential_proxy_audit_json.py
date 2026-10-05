"""The broker's log as structured JSON lines, and the tool-execution audit records in it.

Two properties: every line the broker writes is one JSON object, whatever a
caller or a traceback puts in it; and a tool-execution record carries the
schema's fields as fields, and never the command's arguments.

Run: python3 -m unittest test_credential_proxy_audit_json -v
"""

import argparse
import contextlib
import io
import json
import logging
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import credential_proxy
import scoped_sa_pool
from credential_proxy import (
    CommandExecutor,
    CredentialProxyHandler,
    JsonLineFormatter,
    Policy,
    ProxyMetrics,
    Rule,
)

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
_ENVELOPE = ("severity", "timestamp", "logger", "message")
_TOOL_AUDIT = ("event_type", "request_id", "principal", "tool", "subcommand", "status")
_SECRET = "SECRETVALUE0123456789"


@contextlib.contextmanager
def _refusing_slot(exc):
    """A request slot that refuses on entry, as the real one does when the broker
    is saturated or the queued caller has gone."""
    raise exc
    yield  # pragma: no cover


class FormatterTest(unittest.TestCase):
    def _format(self, record):
        return JsonLineFormatter().format(record)

    def _record(self, message, *args, level=logging.INFO, **extra):
        record = logging.LogRecord("credential-proxy", level, __file__, 1, message, args, None)
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_a_record_is_one_line_with_the_envelope(self):
        line = self._format(self._record("hello %s", "world"))
        self.assertNotIn("\n", line)
        payload = json.loads(line)
        self.assertEqual([payload[key] for key in ("severity", "logger", "message")], ["INFO", "credential-proxy", "hello world"])
        self.assertRegex(payload["timestamp"], _TIMESTAMP)

    def test_a_newline_in_the_message_stays_inside_the_line(self):
        line = self._format(self._record("first\nsecond %s", "third"))
        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line)["message"], "first\nsecond third")

    def test_the_audit_mapping_is_merged_at_the_top_level(self):
        line = self._format(self._record("exec", audit={"event_type": "tool_execution_audit", "tool": "kubectl", "exit_code": 0}))
        payload = json.loads(line)
        self.assertEqual((payload["event_type"], payload["tool"], payload["exit_code"]), ("tool_execution_audit", "kubectl", 0))

    def test_the_envelope_wins_over_an_audit_key_of_the_same_name(self):
        line = self._format(self._record("m", audit={"message": "forged", "severity": "DEBUG"}))
        payload = json.loads(line)
        self.assertEqual((payload["message"], payload["severity"]), ("m", "INFO"))

    def test_an_exception_is_one_field_on_the_same_line(self):
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord("credential-proxy", logging.ERROR, __file__, 1, "failed", (), __import__("sys").exc_info())
        line = self._format(record)
        self.assertNotIn("\n", line)
        payload = json.loads(line)
        self.assertEqual(payload["severity"], "ERROR")
        self.assertIn("ValueError: boom", payload["exception"])

    def test_a_lone_surrogate_is_escaped_and_the_line_still_encodes(self):
        # What the sanitiser's comment relies on: json.dumps writes the
        # surrogate as an escape, so the line encodes to UTF-8 and the record
        # is kept where a text formatter would drop it.
        line = self._format(self._record("id=%s", "\ud800"))
        self.assertIn("\\ud800", line)
        line.encode("utf-8")
        self.assertEqual(json.loads(line)["message"], "id=\ud800")

    def test_a_value_json_cannot_encode_is_rendered_as_text(self):
        line = self._format(self._record("m", audit={"when": Path("/x")}))
        self.assertEqual(json.loads(line)["when"], "/x")


class _BrokerWithJsonLog(unittest.TestCase):
    """A real broker over TCP, logging through JsonLineFormatter into a buffer."""

    def setUp(self):
        for attribute in (
            "policy", "executor", "enforce_read_only", "max_request_bytes", "authenticator", "metrics", "_request_slot",
        ):
            self.addCleanup(
                self._restore,
                attribute,
                attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        policy_path = Path(self.temp_dir.name) / "policy.json"
        policy_path.write_text(json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8")
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        CredentialProxyHandler.executor = CommandExecutor(
            timeout_seconds=5, max_output_bytes=4096,
            state_dir=str(Path(self.temp_dir.name) / "state"), scoped_pool=None,
        )
        stub_dir = Path(self.temp_dir.name) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "kubectl"
        stub.write_text("#!/bin/bash\necho pods\n", encoding="utf-8")
        stub.chmod(0o755)
        CredentialProxyHandler.executor.executables["kubectl"] = str(stub)
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.authenticator = credential_proxy.NullAuthenticator()
        CredentialProxyHandler.metrics = ProxyMetrics()

        self.buffer = io.StringIO()
        handler = logging.StreamHandler(self.buffer)
        handler.setFormatter(JsonLineFormatter())
        previous = list(credential_proxy.LOGGER.handlers)
        propagate = credential_proxy.LOGGER.propagate
        level = credential_proxy.LOGGER.level
        credential_proxy.LOGGER.handlers = [handler]
        credential_proxy.LOGGER.propagate = False
        credential_proxy.LOGGER.setLevel(logging.INFO)
        self.addCleanup(credential_proxy.LOGGER.setLevel, level)
        self.addCleanup(setattr, credential_proxy.LOGGER, "propagate", propagate)
        self.addCleanup(setattr, credential_proxy.LOGGER, "handlers", previous)

        # The class `serve` opens on TCP, so its error hook is under test too.
        self.server = credential_proxy.ThreadingTCPHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    @staticmethod
    def _restore(attribute, present, value):
        if present:
            setattr(CredentialProxyHandler, attribute, value)
        else:
            with contextlib.suppress(AttributeError):
                delattr(CredentialProxyHandler, attribute)

    def post(self, argv, request_id="req-1"):
        request = urllib.request.Request(
            self.endpoint + "/v1/exec",
            data=json.dumps({"requestId": request_id, "argv": argv}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def lines(self):
        return [line for line in self.buffer.getvalue().splitlines() if line]

    def records(self):
        return [json.loads(line) for line in self.lines()]

    def audits(self):
        return [r for r in self.records() if r.get("event_type") == "tool_execution_audit"]


class ToolExecutionAuditTest(_BrokerWithJsonLog):
    def test_every_line_is_json_and_a_completed_command_leaves_two_records(self):
        status, body = self.post(["kubectl", "get", "pods"])
        self.assertEqual((200, "completed"), (status, body["status"]))
        for line in self.lines():
            json.loads(line)
        for record in self.records():
            for key in _ENVELOPE:
                self.assertIn(key, record, record)
        audits = self.audits()
        self.assertEqual([a["status"] for a in audits], ["started", "completed"])
        for record in audits:
            for key in _TOOL_AUDIT:
                self.assertIn(key, record, record)
            self.assertEqual((record["tool"], record["subcommand"], record["request_id"]), ("kubectl", "get", "req-1"))
            self.assertRegex(record["timestamp"], _TIMESTAMP)
        completed = audits[1]
        self.assertEqual(completed["exit_code"], 0)
        self.assertIsInstance(completed["duration_ms"], int)
        self.assertIn("command complete", completed["message"])
        self.assertIn("exec request_id=", audits[0]["message"])

    def test_a_refused_command_is_a_blocked_record_naming_the_rule(self):
        status, _ = self.post(["kubectl", "delete", "pod", "x"], request_id="req-2")
        self.assertEqual(403, status)
        blocked = [a for a in self.audits() if a["status"] == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual((blocked[0]["tool"], blocked[0]["subcommand"], blocked[0]["rule"]), ("kubectl", "delete", "kubernetes.read-only"))
        self.assertEqual(blocked[0]["severity"], "WARNING")

    def test_a_credential_on_the_command_line_reaches_no_record(self):
        # --token is an identity flag the policy refuses, and the refusal names
        # the flag; neither the refusal nor the audit records carry its value.
        status, _ = self.post(["kubectl", "get", "pods", "--token", _SECRET], request_id="req-3")
        self.assertEqual(403, status)
        self.post(["gcloud", "auth", "activate-service-account", f"--password={_SECRET}"], request_id="req-4")
        text = self.buffer.getvalue()
        self.assertNotIn(_SECRET, text)
        self.assertIn("req-3", text)
        for record in self.audits():
            self.assertNotIn("argv", record)
            self.assertNotIn("args", record)

    def test_an_unserved_executable_is_audited_as_other(self):
        self.post(["bash", "-c", "id"], request_id="req-5")
        audits = [a for a in self.audits() if a["request_id"] == "req-5"]
        self.assertEqual([a["status"] for a in audits], ["started", "blocked"])
        self.assertEqual((audits[1]["tool"], audits[1]["subcommand"], audits[1]["rule"]), ("other", "other", "executable.allowlist"))
        # The plain message still names the executable for the reader of the
        # log; the structured fields do not, so a series or a filter on `tool`
        # cannot be grown by what a caller names.
        self.assertIn("bash", audits[1]["message"])


class EveryOutcomeIsAuditedTest(_BrokerWithJsonLog):
    """Each exec-route outcome leaves a tool_execution_audit record with its status and,
    on a refusal, its rule; a refusal added later without the mapping would break here."""

    class _Raising:
        ALLOWED_EXECUTABLES = CommandExecutor.ALLOWED_EXECUTABLES

        def git_lease_violation(self, argv, cwd):
            return None

        def execute(self, argv, stdin=None, cwd=None, kubeconfig_context=None, wants_kubeconfig=False, caller=None):
            raise RuntimeError("the broker fell over")

    class _Abandoning(_Raising):
        def execute(self, argv, stdin=None, cwd=None, kubeconfig_context=None, wants_kubeconfig=False, caller=None):
            return credential_proxy.ExecutionResult(
                exit_code=-9, stdout="", stderr="", duration_ms=250, truncated=False, timed_out=False, abandoned=True,
            )

    def _last_audit(self, request_id):
        records = [a for a in self.audits() if a["request_id"] == request_id]
        self.assertTrue(records, f"no audit record for {request_id}: {self.lines()}")
        return records[-1]

    def test_a_command_the_policy_file_refuses(self):
        # The denylist site, distinct from the read-only gate: the fixture's
        # policy has no rules, so this one installs a rule of its own.
        CredentialProxyHandler.policy = Policy(
            rules=[Rule("kubernetes.secret-read", re.compile(r"kubectl\s+get\s+secrets?\b", re.IGNORECASE), "no")],
            blocked_message="blocked",
        )
        status, body = self.post(["kubectl", "get", "secrets"], request_id="req-p1")
        self.assertEqual((403, "kubernetes.secret-read"), (status, body["rule"]))
        record = self._last_audit("req-p1")
        self.assertEqual(
            (record["status"], record["rule"], record["tool"], record["subcommand"], record["severity"]),
            ("blocked", "kubernetes.secret-read", "kubectl", "get", "WARNING"),
        )

    def test_git_is_refused_on_the_exec_route_and_audited_as_git(self):
        # The exec route runs gcloud and kubectl only; git reaches the broker
        # through the workspace routes. Its refusal comes from the allowlist,
        # before the git argument and lease gates, and is still counted under
        # its own name rather than as `other`.
        status, body = self.post(["git", "-c", "core.hooksPath=/x", "status"], request_id="req-g1")
        self.assertEqual((403, "executable.allowlist"), (status, body["rule"]))
        record = self._last_audit("req-g1")
        self.assertEqual(
            (record["status"], record["rule"], record["tool"]),
            ("blocked", "executable.allowlist", "git"),
        )

    def _route_git(self):
        # Put git back on the exec route to reach the gates behind the
        # allowlist: they are what stands if it is ever admitted there again.
        patcher = mock.patch.object(
            credential_proxy, "EXEC_ROUTE_EXECUTABLES", (*credential_proxy.EXEC_ROUTE_EXECUTABLES, "git"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_refused_git_argument(self):
        self._route_git()
        status, body = self.post(["git", "-c", "core.hooksPath=/x", "status"], request_id="req-g2")
        self.assertEqual((403, "git.argument.refused"), (status, body["rule"]))
        record = self._last_audit("req-g2")
        self.assertEqual(
            (record["status"], record["rule"], record["tool"]),
            ("blocked", "git.argument.refused", "git"),
        )

    def test_a_refused_git_lease(self):
        self._route_git()
        status, body = self.post(["git", "commit", "-m", "x"], request_id="req-l1")
        self.assertEqual((403, "git.workspace.lease"), (status, body["rule"]))
        record = self._last_audit("req-l1")
        self.assertEqual(
            (record["status"], record["rule"], record["tool"], record["subcommand"]),
            ("blocked", "git.workspace.lease", "git", "commit"),
        )

    def test_a_rejected_request(self):
        request = urllib.request.Request(
            self.endpoint + "/v1/exec",
            data=json.dumps({"requestId": "req-r1", "argv": ["kubectl", "get", "pods"], "cwd": "/etc"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(400, caught.exception.code)
        self.assertEqual("rejected", self._last_audit("req-r1")["status"])

    def test_a_broker_fault(self):
        CredentialProxyHandler.executor = self._Raising()
        status, _ = self.post(["kubectl", "get", "pods"], request_id="req-f1")
        self.assertEqual(500, status)
        record = self._last_audit("req-f1")
        self.assertEqual(("failed", "ERROR"), (record["status"], record["severity"]))
        self.assertIn("RuntimeError", record["exception"])

    def test_an_abandoned_command(self):
        CredentialProxyHandler.executor = self._Abandoning()
        request = urllib.request.Request(
            self.endpoint + "/v1/exec",
            data=json.dumps({"requestId": "req-a1", "argv": ["kubectl", "get", "pods"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(request, timeout=5)
        record = self._last_audit("req-a1")
        self.assertEqual(("abandoned", 250), (record["status"], record["duration_ms"]))

    class _Idle(_Raising):
        def execute(self, *args, **kwargs):
            raise AssertionError("a command that never got a slot must not run")

    class _Refusing(_Raising):
        def __init__(self, exc):
            self.exc = exc

        def execute(self, *args, **kwargs):
            raise self.exc

    def _post_raw(self, request_id, argv, cwd):
        request = urllib.request.Request(
            self.endpoint + "/v1/exec",
            data=json.dumps({"requestId": request_id, "argv": argv, "cwd": cwd}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        return caught.exception.code

    def test_a_git_request_with_a_cwd_no_path_can_hold(self):
        # An embedded NUL would raise ValueError out of the git gate's path
        # resolution; git is refused at the allowlist first, so the request
        # ends with a response and the trail with a terminal record, never a
        # fault.
        status = self._post_raw("req-n1", ["git", "status"], "/opt/data/\u0000")
        self.assertEqual(403, status)
        statuses = [a["status"] for a in self.audits() if a["request_id"] == "req-n1"]
        self.assertEqual(["started", "blocked"], statuses)
        self.assertEqual([], [r["message"] for r in self.records() if r["severity"] == "ERROR"])

    def test_a_routed_git_request_with_a_cwd_no_path_can_hold(self):
        # With git admitted, the NUL reaches the git gate's path resolution;
        # it must still end as a rejection, never a fault.
        self._route_git()
        status = self._post_raw("req-n2", ["git", "status"], "/opt/data/\u0000")
        self.assertEqual(400, status)
        statuses = [a["status"] for a in self.audits() if a["request_id"] == "req-n2"]
        self.assertEqual(["started", "rejected"], statuses)
        self.assertEqual([], [r["message"] for r in self.records() if r["severity"] == "ERROR"])

    def test_a_git_request_with_a_cwd_the_broker_cannot_read(self):
        # Under a directory the broker may not stat, the gate's alias lookup
        # raises PermissionError on interpreters up to 3.12; on 3.13, or as
        # root, the lookup reads through and the lease refusal answers. Either
        # way: a response, a terminal record, no fault.
        self._route_git()
        unreadable = Path(self.temp_dir.name) / "unreadable"
        unreadable.mkdir(mode=0o000)
        self.addCleanup(unreadable.chmod, 0o700)
        status = self._post_raw("req-u1", ["git", "foo"], str(unreadable / "repo"))
        self.assertIn(status, (400, 403))
        statuses = [a["status"] for a in self.audits() if a["request_id"] == "req-u1"]
        self.assertEqual(2, len(statuses), statuses)
        self.assertEqual("started", statuses[0])
        self.assertIn(statuses[1], ("rejected", "blocked"))
        self.assertEqual([], [r["message"] for r in self.records() if r["severity"] == "ERROR"])

    def test_a_saturated_broker(self):
        CredentialProxyHandler.executor = self._Idle()
        CredentialProxyHandler._request_slot = lambda handler: _refusing_slot(
            credential_proxy.CommandSlotUnavailable("limit of 8 concurrent commands")
        )
        status, body = self.post(["kubectl", "get", "pods"], request_id="req-b1")
        self.assertEqual((503, "busy"), (status, body["status"]))
        record = self._last_audit("req-b1")
        self.assertEqual(("busy", "WARNING"), (record["status"], record["severity"]))
        self.assertNotIn("duration_ms", record)

    def test_a_caller_that_leaves_the_queue(self):
        CredentialProxyHandler.executor = self._Idle()
        CredentialProxyHandler._request_slot = lambda handler: _refusing_slot(credential_proxy.CallerHungUp())
        request = urllib.request.Request(
            self.endpoint + "/v1/exec",
            data=json.dumps({"requestId": "req-h1", "argv": ["kubectl", "get", "pods"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # Nothing is written back; the record is the only trace of the request.
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(request, timeout=5)
        record = self._last_audit("req-h1")
        self.assertEqual("abandoned", record["status"])
        self.assertNotIn("duration_ms", record)

    def test_a_scoped_credential_refusal(self):
        CredentialProxyHandler.executor = self._Refusing(scoped_sa_pool.PoolRefusal("no member covers the scope"))
        status, body = self.post(["kubectl", "get", "pods"], request_id="req-s1")
        self.assertEqual((403, "gcp.scoped-sa.unmapped-scope"), (status, body["rule"]))
        record = self._last_audit("req-s1")
        self.assertEqual(("blocked", "gcp.scoped-sa.unmapped-scope"), (record["status"], record["rule"]))


class ListenerFaultsAreJsonTest(_BrokerWithJsonLog):
    """What escapes a request handler is a record on the JSON log, never a traceback
    on stderr: the container's two streams are one log, and a traceback is several
    lines a reader expecting one object per line cannot parse."""

    def _wait_for_line(self, fragment, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if any(fragment in line for line in self.lines()):
                return
            time.sleep(0.01)
        self.fail(f"no log line containing {fragment!r}: {self.lines()}")

    def test_an_exception_out_of_a_handler_is_one_record_with_the_traceback_inside(self):
        def bug(handler):
            raise RuntimeError("handler bug")

        CredentialProxyHandler.do_PUT = bug
        self.addCleanup(delattr, CredentialProxyHandler, "do_PUT")
        request = urllib.request.Request(self.endpoint + "/v1/exec", data=b"{}", method="PUT")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            # The connection closes only after the server's error hook has run.
            with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
                urllib.request.urlopen(request, timeout=5)
        self.assertEqual("", stderr.getvalue())
        faults = [r for r in self.records() if "exception" in r]
        self.assertEqual(1, len(faults), self.lines())
        self.assertEqual(
            ("ERROR", "request handler failed type=RuntimeError"), (faults[0]["severity"], faults[0]["message"])
        )
        self.assertIn("handler bug", faults[0]["exception"])

    @staticmethod
    def _reset_mid_request(port, partial_request_line):
        # A reset rather than a close: the read in progress fails instead of
        # ending with a short line, which is the case the guards are for.
        peer = socket.create_connection(("127.0.0.1", port))
        peer.sendall(partial_request_line)  # the rest never comes
        peer.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        peer.close()

    def test_a_peer_that_resets_mid_request_is_a_debug_line(self):
        credential_proxy.LOGGER.setLevel(logging.DEBUG)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self._reset_mid_request(self.server.server_port, b"POST /v1/ex")
            self._wait_for_line("request not answered")
        self.assertEqual("", stderr.getvalue())
        self.assertIn("request not answered type=ConnectionResetError", [r["message"] for r in self.records()])

    def test_a_reset_on_the_api_proxy_listener_is_a_debug_line_too(self):
        # The third handler in the process, on the listener the Service
        # targets in the combined role: its server has the error hook too, so
        # without its own guard a client reset would be logged as a fault.
        credential_proxy.LOGGER.setLevel(logging.DEBUG)
        proxy = credential_proxy.ThreadingTCPHTTPServer(("127.0.0.1", 0), credential_proxy.AgentAPIProxyHandler)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self._reset_mid_request(proxy.server_port, b"GET /hea")
            self._wait_for_line("api request not answered")
        self.assertEqual("", stderr.getvalue())
        records = self.records()
        self.assertIn("api request not answered type=ConnectionResetError", [r["message"] for r in records])
        self.assertEqual([], [r["message"] for r in records if r["severity"] == "ERROR"])


class HostileInputUnderTheJsonFormatterTest(unittest.TestCase):
    """The two byte-level properties the text-formatter tests guard, under the
    formatter the broker ships: a lone surrogate must not drop the record, and a
    control character in the request line must not start a second one."""

    class _RecordingExecutor:
        ALLOWED_EXECUTABLES = CommandExecutor.ALLOWED_EXECUTABLES

        def __init__(self):
            self.executed = []

        def git_lease_violation(self, argv, cwd):
            return None

        def execute(self, argv, stdin=None, cwd=None, kubeconfig_context=None, wants_kubeconfig=False, caller=None):
            self.executed.append(argv)
            return credential_proxy.ExecutionResult(exit_code=0, stdout="", stderr="", duration_ms=0, truncated=False, timed_out=False)

    def setUp(self):
        previous = {name: CredentialProxyHandler.__dict__.get(name) for name in ("executor", "policy", "metrics", "max_request_bytes", "enforce_read_only", "authenticator")}
        for name, value in previous.items():
            self.addCleanup(setattr, CredentialProxyHandler, name, value)
        self.executor = self._RecordingExecutor()
        CredentialProxyHandler.executor = self.executor
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.metrics = ProxyMetrics()
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.authenticator = credential_proxy.NullAuthenticator()

        # errors="strict" on purpose: a real encoder must accept every record.
        self.raw = io.BytesIO()
        stream = io.TextIOWrapper(self.raw, encoding="utf-8", errors="strict", write_through=True)
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter())
        self.emitted = []

        class Counter(logging.Handler):
            def emit(inner, record):  # noqa: N805
                self.emitted.append(record)

        previous_handlers = list(credential_proxy.LOGGER.handlers)
        propagate = credential_proxy.LOGGER.propagate
        level = credential_proxy.LOGGER.level
        credential_proxy.LOGGER.handlers = [handler, Counter()]
        credential_proxy.LOGGER.propagate = False
        credential_proxy.LOGGER.setLevel(logging.INFO)
        self.addCleanup(credential_proxy.LOGGER.setLevel, level)
        self.addCleanup(setattr, credential_proxy.LOGGER, "propagate", propagate)
        self.addCleanup(setattr, credential_proxy.LOGGER, "handlers", previous_handlers)

        self.server = credential_proxy.ThreadingTCPHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def lines(self):
        return [line for line in self.raw.getvalue().decode("utf-8").splitlines() if line]

    def test_a_lone_surrogate_does_not_drop_the_audit_records(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/exec",
            data=b'{"requestId":"\\ud800","argv":["kubectl","get","pods"],"cwd":"/tmp"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(200, response.status)
        self.assertEqual([["kubectl", "get", "pods"]], self.executor.executed)
        records = [json.loads(line) for line in self.lines()]
        statuses = [r["status"] for r in records if r.get("event_type") == "tool_execution_audit"]
        self.assertEqual(["started", "completed"], statuses, records)
        self.assertEqual(len(self.emitted), len(records))

    def test_the_request_line_cannot_start_a_second_record(self):
        connection = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(connection.close)
        connection.sendall(
            b"GET\x0b{\"event_type\":\"tool_execution_audit\",\"principal\":\"someone-else\"}"
            b" /healthz HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        connection.settimeout(2)
        try:
            connection.recv(4096)
        except OSError:
            pass
        lines = self.lines()
        self.assertEqual(len(self.emitted), len(lines), lines)
        for line in lines:
            record = json.loads(line)
            self.assertNotEqual(record.get("principal"), "someone-else", line)
        self.assertTrue(any("someone-else" in line for line in lines), "the request line is still logged, inside its record")



class StartupRefusalIsJsonTest(unittest.TestCase):
    """A refusal to start is one ERROR record on the JSON log with the traceback
    inside it and a non-zero exit, not the interpreter's plain-text traceback."""

    def setUp(self):
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)
        self.addCleanup(setattr, root, "handlers", list(root.handlers))

    @staticmethod
    def _records(text):
        return [json.loads(line) for line in text.splitlines() if line]

    def test_a_refusal_to_start_is_one_error_record(self):
        buffer = io.StringIO()
        refusal = RuntimeError("unsupported CREDENTIAL_PROXY_ROLE 'bogus'")
        with mock.patch.object(credential_proxy, "parse_args", return_value=argparse.Namespace()), \
                mock.patch.object(credential_proxy, "serve", side_effect=refusal), \
                mock.patch.dict(os.environ, {"LOG_LEVEL": "INFO"}):
            status = credential_proxy.main(stream=buffer)
        self.assertEqual(credential_proxy.EXIT_STARTUP_FAILURE, status)
        records = self._records(buffer.getvalue())
        self.assertEqual(1, len(records), records)
        self.assertEqual(
            ("ERROR", "credential proxy failed to start type=RuntimeError"),
            (records[0]["severity"], records[0]["message"]),
        )
        self.assertIn("CREDENTIAL_PROXY_ROLE", records[0]["exception"])

    def test_an_unrecognised_log_level_logs_at_info_and_says_so(self):
        buffer = io.StringIO()
        with mock.patch.object(credential_proxy, "parse_args", return_value=argparse.Namespace()), \
                mock.patch.object(credential_proxy, "serve", return_value=None), \
                mock.patch.dict(os.environ, {"LOG_LEVEL": "loud"}):
            status = credential_proxy.main(stream=buffer)
        self.assertEqual(0, status)
        self.assertEqual(logging.INFO, logging.getLogger().level)
        records = self._records(buffer.getvalue())
        self.assertEqual(["WARNING"], [r["severity"] for r in records], records)
        self.assertIn("LOG_LEVEL='LOUD' names no log level", records[0]["message"])

    def test_the_script_writes_the_refusal_as_json_and_nothing_on_stderr(self):
        # The real entry point in a fresh interpreter: the interpreter's own
        # hook is what would print the traceback, and no in-process test
        # reaches it.
        completed = subprocess.run(
            [sys.executable, credential_proxy.__file__],
            env={**os.environ, "CREDENTIAL_PROXY_ROLE": "bogus"},
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(credential_proxy.EXIT_STARTUP_FAILURE, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        records = self._records(completed.stdout)
        self.assertTrue(records, completed.stdout)
        self.assertEqual("credential proxy failed to start type=RuntimeError", records[-1]["message"])
        self.assertIn("'bogus'", records[-1]["exception"])


if __name__ == "__main__":
    unittest.main()
