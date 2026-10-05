"""The credential broker's Prometheus surface.

A metrics-only listener serves three families: brokered tool invocations by
tool, subcommand and outcome; their wall-clock latency; and the credentialed
listener's requests by route family and status code. Two properties carry the
security argument and are what these tests hold: nothing a caller sends reaches
a label value, and the listener serves nothing but the exposition.

Run: python3 -m unittest test_credential_proxy_metrics -v
"""

import contextlib
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import command_policy
import credential_proxy
import scoped_sa_pool
from credential_proxy import (
    CommandExecutor,
    CredentialProxyHandler,
    MetricsHandler,
    Policy,
    ProxyMetrics,
)

# The vocabulary every label value must come from: lower-case words and
# hyphens for tool, subcommand and status; a route prefix or `other` for the
# endpoint; three digits for the status code. A caller-supplied string that
# slipped into a label would fail all three.
_WORD_LABEL = re.compile(r"^[a-z0-9-]{1,32}$")
_ENDPOINT_LABEL = re.compile(r"^(/[a-z0-9/-]+|other)$")
_STATUS_CODE_LABEL = re.compile(r"^[0-9]{3}$")
_SERIES = re.compile(r"^(?P<name>[a-z_]+)(?:\{(?P<labels>[^}]*)\})? (?P<value>-?[0-9.]+(?:e[+-]?[0-9]+)?)$")
_LABEL_PAIR = re.compile(r'([a-z_]+)="((?:[^"\\]|\\.)*)"')
_STUB_FAILING_EXIT = 3


def _parse(exposition):
    """The exposition as {name: {frozenset(label pairs): value}}, checking its shape."""
    families = {}
    typed = set()
    for line in exposition.splitlines():
        if line.startswith("# TYPE "):
            typed.add(line.split()[2])
            continue
        if line.startswith("#"):
            continue
        match = _SERIES.match(line)
        assert match, f"not a Prometheus text line: {line!r}"
        labels = frozenset(_LABEL_PAIR.findall(match.group("labels") or ""))
        families.setdefault(match.group("name"), {})[labels] = float(match.group("value"))
    for name in families:
        base = re.sub(r"_(bucket|sum|count)$", "", name)
        assert base in typed, f"{name} has no # TYPE line"
    return families


def _series(families, name, **labels):
    return families.get(name, {}).get(frozenset(labels.items()))


class _BrokerFixture(unittest.TestCase):
    """A real broker over TCP with a stub kubectl, and a fresh registry per test."""

    def setUp(self):
        for attribute in ("policy", "executor", "enforce_read_only", "max_request_bytes", "authenticator", "metrics"):
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
            timeout_seconds=5,
            max_output_bytes=4096,
            state_dir=str(Path(self.temp_dir.name) / "state"),
            scoped_pool=None,
        )
        stub_dir = Path(self.temp_dir.name) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "kubectl"
        # `kubectl get failing` exits non-zero through an allowed verb, so the
        # command runs and fails rather than being refused before it starts.
        stub.write_text(
            "#!/bin/bash\n"
            f'case "$*" in *failing*) exit {_STUB_FAILING_EXIT} ;; esac\n'
            "echo pods\n",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        CredentialProxyHandler.executor.executables["kubectl"] = str(stub)
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.authenticator = credential_proxy.NullAuthenticator()
        CredentialProxyHandler.metrics = ProxyMetrics()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
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

    def post(self, argv, **extra):
        payload = {"requestId": "req-1", "argv": argv, **extra}
        request = urllib.request.Request(
            self.endpoint + "/v1/exec",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def get(self, path):
        try:
            with urllib.request.urlopen(self.endpoint + path) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def families(self):
        return _parse(CredentialProxyHandler.metrics.render())


class ToolInvocationCountingTest(_BrokerFixture):
    def test_a_completed_command_counts_as_success_and_is_timed(self):
        status, body = self.post(["kubectl", "get", "pods"])
        self.assertEqual((200, "completed", 0), (status, body["status"], body["exitCode"]))
        families = self.families()
        self.assertEqual(
            1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="success")
        )
        self.assertEqual(1, _series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))
        self.assertEqual(1, _series(families, "kubeagents_tool_execution_duration_seconds_bucket", tool="kubectl", le="+Inf"))

    def test_a_non_zero_exit_counts_as_error(self):
        # The response still says `completed`: it reports that the broker ran
        # the command, the counter reports how the command went.
        status, body = self.post(["kubectl", "get", "failing"])
        self.assertEqual((200, "completed", _STUB_FAILING_EXIT), (status, body["status"], body["exitCode"]))
        families = self.families()
        self.assertEqual(
            1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="error")
        )
        self.assertIsNone(
            _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="success")
        )

    def test_a_refused_command_counts_as_blocked_under_its_verb_and_is_not_timed(self):
        status, body = self.post(["kubectl", "delete", "pod", "x"])
        self.assertEqual((403, "blocked"), (status, body["status"]))
        families = self.families()
        self.assertEqual(
            1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="delete", status="blocked")
        )
        self.assertNotIn("kubeagents_tool_execution_duration_seconds_count", families)

    def test_an_unserved_executable_is_counted_as_other_and_never_named(self):
        status, body = self.post(["bash", "-c", "id"])
        self.assertEqual((403, "executable.allowlist"), (status, body["rule"]))
        exposition = CredentialProxyHandler.metrics.render()
        self.assertNotIn("bash", exposition)
        self.assertEqual(
            1, _series(_parse(exposition), "kubeagents_tool_invocations_total", tool="other", subcommand="other", status="blocked")
        )

    def test_caller_text_never_reaches_a_label(self):
        # A verb the vocabulary does not list, a namespace with a quote in it,
        # and a flag the policy does not know: each ends up under `other` and
        # none of the caller's own strings appears in the exposition.
        self.post(["kubectl", 'weirdverb"x', "pods"])
        self.post(["kubectl", "get", "pods", "--namespace", 'evil"ns'])
        self.post(["kubectl", "--nosuchflag", "value", "get", "pods"])
        exposition = CredentialProxyHandler.metrics.render()
        for text in ("weirdverb", "evil", "nosuchflag"):
            self.assertNotIn(text, exposition)
        families = _parse(exposition)
        for labels in families["kubeagents_tool_invocations_total"]:
            for key, value in labels:
                self.assertRegex(value, _WORD_LABEL, f"{key}={value!r} is not vocabulary")
        self.assertGreaterEqual(
            _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="other", status="blocked"),
            2,
            exposition,
        )

    def test_a_policy_rule_match_counts_as_blocked_under_its_verb(self):
        policy_path = Path(self.temp_dir.name) / "policy-with-rule.json"
        policy_path.write_text(
            json.dumps({"blockedMessage": "blocked", "rules": [
                {"id": "kubernetes.no-secrets", "pattern": r"\bkubectl\b.*\bsecrets?\b", "message": "no secrets"},
            ]}),
            encoding="utf-8",
        )
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        status, body = self.post(["kubectl", "get", "secrets"])
        self.assertEqual(403, status)
        self.assertIn("kubernetes.no-secrets", json.dumps(body))
        families = self.families()
        self.assertEqual(1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="blocked"))
        self.assertIsNone(_series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))

    def routed_git(self):
        # `/v1/exec` refuses `git` before either git gate, so admit it to count
        # what those gates refuse.
        routed = (*credential_proxy.EXEC_ROUTE_EXECUTABLES, "git")
        return mock.patch.object(credential_proxy, "EXEC_ROUTE_EXECUTABLES", routed)

    def test_git_counts_as_blocked_on_the_exec_route(self):
        status, body = self.post(["git", "status"])
        self.assertEqual(403, status)
        self.assertEqual("executable.allowlist", body.get("rule"))
        self.assertEqual(1, _series(self.families(), "kubeagents_tool_invocations_total", tool="git", subcommand="status", status="blocked"))

    def test_a_refused_git_argument_counts_as_blocked(self):
        with self.routed_git():
            status, body = self.post(["git", "-c", "x=y", "status"])
        self.assertEqual(403, status)
        self.assertEqual("git.argument.refused", body.get("rule"))
        self.assertEqual(1, _series(self.families(), "kubeagents_tool_invocations_total", tool="git", subcommand="status", status="blocked"))

    def test_a_git_write_outside_a_lease_counts_as_blocked(self):
        # No cwd, so the command would run at the shared workspace root, which
        # the lease floor refuses for a write.
        with self.routed_git():
            status, body = self.post(["git", "commit", "-m", "x"])
        self.assertEqual(403, status)
        self.assertEqual("git.workspace.lease", body.get("rule"))
        self.assertEqual(1, _series(self.families(), "kubeagents_tool_invocations_total", tool="git", subcommand="commit", status="blocked"))

    def test_a_cwd_outside_the_workspace_counts_as_error_and_is_not_timed(self):
        status, _ = self.post(["kubectl", "get", "pods"], cwd="/etc")
        self.assertEqual(400, status)
        families = self.families()
        self.assertEqual(1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="error"))
        self.assertIsNone(_series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/v1/exec", status_code="400"))


class AbandonedCommandTest(unittest.TestCase):
    """A command the caller hung up on ran and was killed: counted and timed under
    its own outcome, since no response is written for log_request to count."""

    def test_an_abandoned_command_is_counted_and_timed(self):
        class _Abandoning:
            ALLOWED_EXECUTABLES = CommandExecutor.ALLOWED_EXECUTABLES

            def git_lease_violation(self, argv, cwd):
                return None

            def execute(self, argv, stdin=None, cwd=None, kubeconfig_context=None, wants_kubeconfig=False, caller=None):
                return credential_proxy.ExecutionResult(
                    exit_code=-9, stdout="", stderr="", duration_ms=1500, truncated=False, timed_out=False, abandoned=True,
                )

        previous = {name: CredentialProxyHandler.__dict__.get(name) for name in ("executor", "policy", "metrics", "max_request_bytes", "enforce_read_only", "authenticator")}
        for name, value in previous.items():
            self.addCleanup(setattr, CredentialProxyHandler, name, value)
        CredentialProxyHandler.executor = _Abandoning()
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.metrics = ProxyMetrics()
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.authenticator = credential_proxy.NullAuthenticator()
        server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/exec",
            data=json.dumps({"requestId": "req-a", "argv": ["kubectl", "get", "pods"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        # The handler returns without writing, so the client sees the connection
        # close with no status line.
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(request, timeout=5)
        families = _parse(CredentialProxyHandler.metrics.render())
        self.assertEqual(1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="abandoned"))
        self.assertEqual(1, _series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))
        self.assertIsNone(_series(families, "kubeagents_credential_proxy_requests_total", endpoint="/v1/exec", status_code="200"))


class RequestCountingTest(_BrokerFixture):
    def test_requests_are_counted_by_route_family_and_status_code(self):
        self.post(["kubectl", "get", "pods"])
        self.post(["kubectl", "delete", "pod", "x"])
        self.assertEqual(200, self.get("/healthz"))
        self.assertEqual(404, self.get("/no/such/route"))
        families = self.families()
        counts = families["kubeagents_credential_proxy_requests_total"]
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/v1/exec", status_code="200"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/v1/exec", status_code="403"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/healthz", status_code="200"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="other", status_code="404"))
        for labels in counts:
            pairs = dict(labels)
            self.assertRegex(pairs["endpoint"], _ENDPOINT_LABEL)
            self.assertRegex(pairs["status_code"], _STATUS_CODE_LABEL)

    def test_the_path_itself_is_never_a_label(self):
        self.get("/v1/gcp/monitoring.googleapis.com/v3/projects/secret-project/timeSeries?filter=x")
        exposition = CredentialProxyHandler.metrics.render()
        self.assertNotIn("secret-project", exposition)
        self.assertNotIn("timeSeries", exposition)
        self.assertIn('endpoint="/v1/gcp"', exposition)


class MetricsListenerTest(unittest.TestCase):
    def setUp(self):
        previous = CredentialProxyHandler.__dict__.get("metrics")
        self.addCleanup(setattr, CredentialProxyHandler, "metrics", previous)
        CredentialProxyHandler.metrics = ProxyMetrics()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MetricsHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"

    def test_metrics_are_served_unauthenticated_in_the_text_exposition(self):
        CredentialProxyHandler.metrics.record_tool("kubectl", "get", "success")
        CredentialProxyHandler.metrics.observe_duration("kubectl", 0.24)
        with urllib.request.urlopen(self.endpoint + "/metrics") as response:
            self.assertEqual(200, response.status)
            self.assertEqual(credential_proxy.METRICS_CONTENT_TYPE, response.headers["Content-Type"])
            self.assertNotIn("Python", response.headers.get("Server", ""))
            body = response.read().decode("utf-8")
        families = _parse(body)
        self.assertEqual(1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="success"))
        # Cumulative buckets, in bound order, ending at the count.
        buckets = [
            _series(families, "kubeagents_tool_execution_duration_seconds_bucket", tool="kubectl", le=str(bound))
            for bound in credential_proxy.TOOL_DURATION_BUCKETS
        ]
        self.assertEqual(buckets, sorted(buckets))
        self.assertEqual(0, buckets[0])
        self.assertEqual(1, buckets[-1])
        self.assertEqual(1, _series(families, "kubeagents_tool_execution_duration_seconds_bucket", tool="kubectl", le="+Inf"))
        self.assertAlmostEqual(0.24, _series(families, "kubeagents_tool_execution_duration_seconds_sum", tool="kubectl"))
        self.assertIn("# HELP kubeagents_credential_proxy_requests_total", body)

    def test_the_listener_serves_nothing_else(self):
        for path in ("/", "/healthz", "/v1/exec", "/metrics/../v1/exec"):
            with self.subTest(path=path):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(self.endpoint + path)
                self.assertEqual(404, caught.exception.code)

    def test_a_scrape_writes_no_access_log_line(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), self.assertNoLogs(credential_proxy.LOGGER, level="DEBUG"):
            urllib.request.urlopen(self.endpoint + "/metrics").read()
        self.assertEqual("", captured.getvalue())

    def test_two_scrapes_of_an_idle_registry_are_identical(self):
        first = urllib.request.urlopen(self.endpoint + "/metrics").read()
        second = urllib.request.urlopen(self.endpoint + "/metrics").read()
        self.assertEqual(first, second)

    def _hung_up_peer(self, request_line):
        """A handler whose peer has gone: the request is readable, every write fails."""
        class _Gone:
            def write(self, data):
                raise BrokenPipeError()

            def flush(self):
                return None

        self.addCleanup(
            _BrokerFixture._restore, "metrics", "metrics" in CredentialProxyHandler.__dict__, CredentialProxyHandler.__dict__.get("metrics")
        )
        CredentialProxyHandler.metrics = ProxyMetrics()
        handler = MetricsHandler.__new__(MetricsHandler)
        handler.client_address = ("127.0.0.1", 0)
        handler.rfile = io.BytesIO(request_line + b"\r\nHost: broker\r\n\r\n")
        handler.wfile = _Gone()
        return handler

    def test_a_scrape_the_collector_abandons_is_not_a_traceback(self):
        handler = self._hung_up_peer(b"GET /metrics HTTP/1.1")
        with self.assertLogs(credential_proxy.LOGGER, level="DEBUG") as logs:
            handler.handle_one_request()
        self.assertTrue(handler.close_connection)
        self.assertTrue(any("metrics request not answered" in line and "BrokenPipeError" in line for line in logs.output), logs.output)

    def test_a_hung_up_peer_on_any_other_path_is_not_a_traceback_either(self):
        # The 404 is written by send_error, outside do_GET's own lines; the
        # guard sits above both, so a probe at `/` from a peer that leaves is
        # the same debug line.
        handler = self._hung_up_peer(b"GET / HTTP/1.1")
        with self.assertLogs(credential_proxy.LOGGER, level="DEBUG") as logs:
            handler.handle_one_request()
        self.assertTrue(handler.close_connection)
        self.assertTrue(any("metrics request not answered" in line for line in logs.output), logs.output)


class ListenerBoundsTest(unittest.TestCase):
    """The listener shares the process with the credentialed handler, so a peer
    that reaches the port gets at most METRICS_MAX_CONNECTIONS threads for at most
    METRICS_CONNECTION_DEADLINE_SECONDS each, whatever it sends."""

    _DEADLINE = 1
    _GRACE = 5

    def _listener(self, timeout, **overrides):
        for name, value in overrides.items():
            patcher = mock.patch.object(credential_proxy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The per-recv timeout MetricsHandler.setup() applies; short where the
        # test is about an idle peer, long where the holders must stay parked.
        timeout_patch = mock.patch.object(MetricsHandler, "timeout", timeout)
        timeout_patch.start()
        self.addCleanup(timeout_patch.stop)
        server = credential_proxy.start_metrics_listener("127.0.0.1", 0)
        self.assertIsInstance(server, credential_proxy.MetricsServer)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def _connect(self, port):
        sock = socket.create_connection(("127.0.0.1", port), timeout=self._GRACE)
        self.addCleanup(sock.close)
        return sock

    @staticmethod
    def _closed_by_server(sock):
        """True once the server has closed the connection: a read returns EOF or fails.

        The client's own timeout is not a close: it is re-raised, so a server
        that parks a connection unanswered fails the test instead of passing it.
        """
        try:
            return sock.recv(1) == b""
        except TimeoutError:
            raise
        except OSError:
            return True

    def test_an_idle_peer_is_dropped_at_the_deadline(self):
        # The per-recv timeout is long here, so the deadline timer is the only
        # thing that can end the connection.
        port = self._listener(self._GRACE, METRICS_CONNECTION_DEADLINE_SECONDS=self._DEADLINE)
        sock = self._connect(port)
        self.assertTrue(self._closed_by_server(sock))

    def test_a_trickling_peer_is_cut_off_at_the_deadline(self):
        port = self._listener(self._DEADLINE, METRICS_CONNECTION_DEADLINE_SECONDS=self._DEADLINE)
        sock = self._connect(port)
        sock.sendall(b"GET /metr")
        started = time.monotonic()
        cut = False
        while time.monotonic() - started < self._GRACE:
            time.sleep(self._DEADLINE / 5)
            try:
                sock.sendall(b"i")
            except OSError:
                cut = True
                break
            sock.settimeout(0.1)
            try:
                if sock.recv(1) == b"":
                    cut = True
                    break
            except TimeoutError:
                continue
            except OSError:
                cut = True
                break
        self.assertTrue(cut, "a peer sending one byte at a time held its thread past the deadline")

    def test_connections_past_the_cap_are_closed_unserved_and_slots_come_back(self):
        port = self._listener(self._GRACE, METRICS_MAX_CONNECTIONS=2, METRICS_CONNECTION_DEADLINE_SECONDS=self._GRACE)
        holders = [self._connect(port) for _ in range(2)]
        time.sleep(1)  # let the listener accept both and park a thread on each
        extra = self._connect(port)
        extra.sendall(b"GET /metrics HTTP/1.1\r\nHost: broker\r\n\r\n")
        self.assertTrue(self._closed_by_server(extra), "a third connection was served past the cap")
        for holder in holders:
            holder.close()
        time.sleep(0.5)  # the parked threads notice EOF and release their slots
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=self._GRACE) as response:
            self.assertEqual(200, response.status)


class ListenerStartTest(unittest.TestCase):
    def test_an_occupied_port_is_logged_not_raised(self):
        with socket.socket() as holder:
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            with self.assertLogs(credential_proxy.LOGGER, level="ERROR") as logs:
                self.assertIsNone(credential_proxy.start_metrics_listener("127.0.0.1", port))
        self.assertTrue(any("ALERT" in line and "/metrics" in line for line in logs.output), logs.output)

    def test_a_port_beyond_the_range_is_logged_not_raised(self):
        # bind() raises OverflowError, not OSError, for this; the guard has
        # to hold for any int or the broker dies at boot with it.
        with self.assertLogs(credential_proxy.LOGGER, level="ERROR") as logs:
            self.assertIsNone(credential_proxy.start_metrics_listener("127.0.0.1", 70000))
        self.assertTrue(any("ALERT" in line and "OverflowError" in line for line in logs.output), logs.output)

    def test_a_free_port_is_served_on_a_daemon_thread(self):
        server = credential_proxy.start_metrics_listener("127.0.0.1", 0)
        self.assertIsNotNone(server)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/metrics") as response:
            self.assertEqual(200, response.status)

    def test_a_value_that_is_not_an_integer_disables_the_listener_and_says_so(self):
        with mock.patch.object(sys, "argv", ["credential_proxy.py"]):
            with mock.patch.dict(os.environ, {credential_proxy.METRICS_PORT_ENV: "8766a"}):
                with self.assertLogs(credential_proxy.LOGGER, level="ERROR") as logs:
                    self.assertEqual(0, credential_proxy.parse_args().metrics_port)
        self.assertTrue(any("ALERT" in line and "8766a" in line for line in logs.output), logs.output)

    def test_the_port_comes_from_the_operators_variable_and_defaults_off(self):
        with mock.patch.object(sys, "argv", ["credential_proxy.py"]):
            with mock.patch.dict(os.environ, {credential_proxy.METRICS_PORT_ENV: ""}):
                self.assertEqual(0, credential_proxy.parse_args().metrics_port)
            with mock.patch.dict(os.environ, {credential_proxy.METRICS_PORT_ENV: "8766"}):
                self.assertEqual(8766, credential_proxy.parse_args().metrics_port)
            os.environ.pop(credential_proxy.METRICS_PORT_ENV, None)
            self.assertEqual(0, credential_proxy.parse_args().metrics_port)


_REPO_ROOT = Path(__file__).resolve().parents[3]
_OPERATOR_MANIFESTS = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
_ENVOY_CONFIG = _REPO_ROOT / "deploy" / "shared" / "envoy-credential-proxy.yaml"


class OperatorContractTest(unittest.TestCase):
    """The operator sets the variable the runtime reads: one name, pinned from
    the runtime's side. The operator's own test pins its literal; without this
    a rename on either side keeps both suites green while the broker logs
    `metrics listener disabled` under a declared port and an open policy."""

    def test_the_operator_names_the_variable_the_runtime_reads(self):
        manifests = _OPERATOR_MANIFESTS.read_text()
        self.assertRegex(
            manifests,
            r'credentialProxyMetricsPortEnv\s*=\s*"' + re.escape(credential_proxy.METRICS_PORT_ENV) + '"',
            f"the operator does not set {credential_proxy.METRICS_PORT_ENV}; the listener is never switched on",
        )

    def test_the_credentialed_port_has_one_number_across_operator_envoy_and_runtime(self):
        # _metrics_port_refusal compares the metrics port against args.port, so
        # args.port has to be the port Envoy binds in front of the socket. The
        # operator sets it from credentialProxyPort, Envoy's config carries the
        # literal, and the runtime's default is the fallback for a hand run:
        # one number, or the refusal guards the wrong port.
        manifests = _OPERATOR_MANIFESTS.read_text()
        self.assertRegex(manifests, r'credentialProxyPortEnv\s*=\s*"CREDENTIAL_PROXY_PORT"')
        operator_port = int(re.search(r"^\s*credentialProxyPort\s*=\s*(\d+)", manifests, re.MULTILINE).group(1))
        envoy_port = int(re.search(r"port_value:\s*(\d+)", _ENVOY_CONFIG.read_text()).group(1))
        with mock.patch.object(sys, "argv", ["credential_proxy.py"]), mock.patch.dict(os.environ):
            os.environ.pop("CREDENTIAL_PROXY_PORT", None)
            runtime_default = credential_proxy.parse_args().port
        self.assertEqual(
            (operator_port, operator_port), (envoy_port, runtime_default),
            f"operator {operator_port}, Envoy {envoy_port}, runtime default {runtime_default}: the refusal guards a port nobody binds",
        )


class LabelDerivationTest(unittest.TestCase):
    def test_tool_and_subcommand_labels(self):
        cases = {
            ("kubectl", "get", "pods"): ("kubectl", "get"),
            ("kubectl", "--namespace", "foo", "get", "pods"): ("kubectl", "get"),
            ("kubectl", "rollout", "status", "deploy/x"): ("kubectl", "rollout"),
            ("kubectl", "apply", "-f", "x.yaml"): ("kubectl", "apply"),
            ("kubectl", "--nosuchflag", "x", "get"): ("kubectl", "other"),
            ("kubectl", "gett", "pods"): ("kubectl", "other"),
            ("kubectl",): ("kubectl", "none"),
            ("gcloud", "container", "clusters", "get-credentials", "c"): ("gcloud", "container"),
            ("gcloud", "beta", "compute", "instances", "list"): ("gcloud", "compute"),
            ("gcloud",): ("gcloud", "none"),
            ("git", "-C", "/tmp/x", "status"): ("git", "status"),
            ("git", "rev-parse", "HEAD"): ("git", "rev-parse"),
            ("git", "not-a-verb"): ("git", "other"),
            ("gh", "pr", "list"): ("gh", "pr"),
            ("gh", "--version"): ("gh", "none"),
            ("bash", "-c", "id"): ("other", "other"),
        }
        for argv, want in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(want, credential_proxy._tool_labels(list(argv)))

    def test_every_vocabulary_word_is_a_valid_label(self):
        for tool in ("kubectl", "gcloud", "git", "gh"):
            for word in credential_proxy._subcommand_vocabulary(tool):
                with self.subTest(tool=tool, word=word):
                    self.assertRegex(word, _WORD_LABEL)

    def test_endpoint_labels(self):
        cases = {
            "/v1/exec": "/v1/exec",
            "/v1/chat/events": "/v1/chat",
            "/v1/chat/a2a/events": "/v1/chat/a2a",
            "/v1/chat/api": "/v1/chat/api",
            "/v1/gcp/monitoring.googleapis.com/v3/x?y=z": "/v1/gcp",
            "/v1/vcs/push": "/v1/vcs",
            "/v1/workspace/acquire": "/v1/workspace",
            "/v1/forge/refresh": "/v1/forge",
            "/healthz": "/healthz",
            "/metrics": "other",
            "": "other",
            "/v1/chatter": "other",
        }
        for path, want in cases.items():
            with self.subTest(path=path):
                self.assertEqual(want, credential_proxy._endpoint_label(path))


class RegistryTest(unittest.TestCase):
    def test_concurrent_increments_are_not_lost(self):
        metrics = ProxyMetrics()
        per_thread, threads = 500, 8

        def work():
            for _ in range(per_thread):
                metrics.record_tool("kubectl", "get", "success")
                metrics.observe_duration("kubectl", 0.01)
                metrics.record_request("/v1/exec", "200")

        workers = [threading.Thread(target=work) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        families = _parse(metrics.render())
        self.assertEqual(per_thread * threads, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="success"))
        self.assertEqual(per_thread * threads, _series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))
        self.assertEqual(per_thread * threads, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/v1/exec", status_code="200"))

    def test_label_values_are_escaped(self):
        metrics = ProxyMetrics()
        metrics.record_request('quote"back\\slash\nnewline', "200")
        rendered = metrics.render()
        self.assertIn('endpoint="quote\\"back\\\\slash\\nnewline"', rendered)
        self.assertEqual(1, len([line for line in rendered.splitlines() if line.startswith("kubeagents_credential_proxy_requests_total{")]))


@contextlib.contextmanager
def _refusing_slot(exc):
    """A request slot that refuses on entry the way the real one does when the
    broker is saturated or the queued caller has gone."""
    raise exc
    yield  # pragma: no cover


class NeverStartedCommandTest(unittest.TestCase):
    """Two outcomes end a command before it starts: the slots stay full for the
    whole wait (a 503), or the caller hangs up while queued (no response at all).
    Both are counted and neither is timed; the busy one is its own status so a
    saturated broker reads as saturated rather than as failing commands."""

    class _Idle:
        ALLOWED_EXECUTABLES = CommandExecutor.ALLOWED_EXECUTABLES

        def git_lease_violation(self, argv, cwd):
            return None

        def execute(self, *args, **kwargs):
            raise AssertionError("a command that never got a slot must not run")

    def _serve_refusing(self, exc):
        names = ("executor", "policy", "metrics", "max_request_bytes", "enforce_read_only", "authenticator", "_request_slot")
        previous = {name: CredentialProxyHandler.__dict__.get(name) for name in names}
        for name, value in previous.items():
            self.addCleanup(setattr, CredentialProxyHandler, name, value)
        CredentialProxyHandler.executor = self._Idle()
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.metrics = ProxyMetrics()
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.authenticator = credential_proxy.NullAuthenticator()
        CredentialProxyHandler._request_slot = lambda handler: _refusing_slot(exc)
        server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/exec",
            data=json.dumps({"requestId": "req-n", "argv": ["kubectl", "get", "pods"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def test_a_saturated_broker_counts_the_command_as_busy_and_does_not_time_it(self):
        request = self._serve_refusing(credential_proxy.CommandSlotUnavailable("limit of 8 concurrent commands"))
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(503, caught.exception.code)
        families = _parse(CredentialProxyHandler.metrics.render())
        self.assertEqual(1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="busy"))
        self.assertIsNone(_series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/v1/exec", status_code="503"))

    def test_a_caller_that_leaves_the_queue_is_counted_as_abandoned(self):
        request = self._serve_refusing(credential_proxy.CallerHungUp())
        # Nothing is written back, so the client sees the connection close.
        with self.assertRaises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(request, timeout=5)
        families = _parse(CredentialProxyHandler.metrics.render())
        self.assertEqual(1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="abandoned"))
        self.assertIsNone(_series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))


class FaultOutcomeTest(unittest.TestCase):
    """The two outcomes that end in the route's exception handlers: a scoped
    service-account pool with no member for the request, answered 403 as a
    refusal, and a broker fault, answered 500. Both are counted before the
    response is written and neither is timed, since the command never ran."""

    class _Raising:
        ALLOWED_EXECUTABLES = CommandExecutor.ALLOWED_EXECUTABLES

        def __init__(self, exc):
            self.exc = exc

        def git_lease_violation(self, argv, cwd):
            return None

        def execute(self, *args, **kwargs):
            raise self.exc

    def _post_with(self, exc):
        names = ("executor", "policy", "metrics", "max_request_bytes", "enforce_read_only", "authenticator")
        previous = {name: CredentialProxyHandler.__dict__.get(name) for name in names}
        for name, value in previous.items():
            self.addCleanup(setattr, CredentialProxyHandler, name, value)
        CredentialProxyHandler.executor = self._Raising(exc)
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.metrics = ProxyMetrics()
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.authenticator = credential_proxy.NullAuthenticator()
        server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/exec",
            data=json.dumps({"requestId": "req-f", "argv": ["kubectl", "get", "pods"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_a_pool_refusal_counts_as_blocked(self):
        status, body = self._post_with(scoped_sa_pool.PoolRefusal("no member covers the scope"))
        self.assertEqual(403, status)
        self.assertEqual("gcp.scoped-sa.unmapped-scope", body.get("rule"))
        families = _parse(CredentialProxyHandler.metrics.render())
        self.assertEqual(1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="blocked"))
        self.assertIsNone(_series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))

    def test_a_broker_fault_counts_as_error(self):
        status, _ = self._post_with(RuntimeError("boom"))
        self.assertEqual(500, status)
        families = _parse(CredentialProxyHandler.metrics.render())
        self.assertEqual(1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="error"))
        self.assertIsNone(_series(families, "kubeagents_tool_execution_duration_seconds_count", tool="kubectl"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_requests_total", endpoint="/v1/exec", status_code="500"))


class PolicyReadCoverageTest(unittest.TestCase):
    """Every read the policy tables allow labels as itself, so a panel over the
    policy's own vocabulary sees every allowed command and `other` means what it
    says. The gcloud table has entries that start with a release track, and the
    label skips the track the way the policy does."""

    def test_every_kubectl_read_verb_labels_as_itself(self):
        for verb in sorted(command_policy.KUBECTL_READ_VERBS):
            with self.subTest(verb=verb):
                self.assertEqual(("kubectl", verb[0]), credential_proxy._tool_labels(["kubectl", *verb]))

    def test_every_gcloud_read_command_labels_by_its_group(self):
        for command in sorted(command_policy.GCLOUD_READ_COMMANDS):
            with self.subTest(command=command):
                group = command_policy._gcloud_surface(list(command))
                self.assertNotIn(group, command_policy._GCLOUD_RELEASE_TRACKS)
                self.assertEqual(("gcloud", group), credential_proxy._tool_labels(["gcloud", *command]))
        vocabulary = credential_proxy._subcommand_vocabulary("gcloud")
        self.assertFalse(vocabulary & command_policy._GCLOUD_RELEASE_TRACKS, "a release track is never a label")

    def test_every_git_verb_a_gate_reads_labels_as_itself(self):
        # The lease gate's list and the workspace's list are the vocabulary's
        # sources, so a verb either refuses or runs under its own name.
        import content_workspace

        verbs = (credential_proxy.GIT_MUTATING_SUBCOMMANDS | content_workspace.WORKSPACE_GIT_SUBCOMMANDS
                 | credential_proxy.VCS_GIT_SUBCOMMANDS | credential_proxy.GIT_READ_SUBCOMMANDS)
        for verb in sorted(verbs):
            with self.subTest(verb=verb):
                self.assertEqual(("git", verb), credential_proxy._tool_labels(["git", verb]))

    def test_a_forge_cli_without_a_vocabulary_reads_other_not_another_tools_verbs(self):
        allowed = set(CommandExecutor.ALLOWED_EXECUTABLES) | {"glab"}
        with mock.patch.object(CommandExecutor, "ALLOWED_EXECUTABLES", allowed):
            self.assertEqual(("glab", credential_proxy.LABEL_OTHER), credential_proxy._tool_labels(["glab", "pr", "list"]))
            self.assertEqual(("gh", "pr"), credential_proxy._tool_labels(["gh", "pr", "list"]))

    def test_a_forge_clis_global_flags_are_stepped_over(self):
        self.assertEqual(("gh", "pr"), credential_proxy._tool_labels(["gh", "-R", "owner/repo", "pr", "list"]))
        self.assertEqual(("gh", "issue"), credential_proxy._tool_labels(["gh", "--repo=owner/repo", "issue", "view", "1"]))
        self.assertEqual(("gh", credential_proxy.SUBCOMMAND_NONE), credential_proxy._tool_labels(["gh", "--repo", "owner/repo"]))


class ServeWiringTest(unittest.TestCase):
    """serve() is the one place the operator's port becomes a listener: the
    parsed port has to reach start_metrics_listener, and a value that is no port
    has to be refused there by name rather than left to bind()."""

    class _Stop(Exception):
        pass

    def _serve(self, metrics_port, env_value=None, started=None, unix_server=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        policy_path = Path(tmp.name) / "policy.json"
        policy_path.write_text(json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8")
        args = types.SimpleNamespace(
            policy=str(policy_path),
            host="127.0.0.1",
            port=0,
            unix_socket=str(Path(tmp.name) / "backend.sock"),
            timeout_seconds=5,
            max_request_bytes=1 << 20,
            max_output_bytes=1 << 20,
            state_dir=str(Path(tmp.name) / "state"),
            role="full",
            metrics_port=metrics_port,
        )
        environment = {
            "API_SERVER_EXTERNAL_KEY": "external",
            "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND": "",
            "CREDENTIAL_PROXY_SCOPED_SA_POOL": "0",
        }
        if env_value is not None:
            environment[credential_proxy.METRICS_PORT_ENV] = env_value
        bound = []
        owner = self

        def stop(server):
            bound.append(server)
            raise owner._Stop

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        started = started if started is not None else mock.MagicMock(return_value=None)
        unix_server = unix_server if unix_server is not None else credential_proxy.ThreadingUnixHTTPServer
        try:
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(credential_proxy, "ThreadingUnixHTTPServer", unix_server), \
                    mock.patch.object(credential_proxy, "ThreadingTCPHTTPServer", mock.MagicMock()), \
                    mock.patch.object(credential_proxy.threading, "Thread", FakeThread), \
                    mock.patch.object(credential_proxy.ThreadingUnixHTTPServer, "serve_forever", stop), \
                    mock.patch.object(credential_proxy, "start_metrics_listener", started), \
                    self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                with self.assertRaises(self._Stop):
                    credential_proxy.serve(args)
        finally:
            for server in bound:
                server.server_close()
        return started, logs.output

    def test_the_parsed_port_reaches_the_listener(self):
        started, _ = self._serve(8766)
        started.assert_called_once_with("127.0.0.1", 8766)

    def test_a_value_that_is_no_port_is_refused_by_name_and_never_bound(self):
        started, logs = self._serve(70000)
        started.assert_not_called()
        self.assertTrue(
            any("ALERT" in line and credential_proxy.METRICS_PORT_ENV in line and "70000" in line for line in logs),
            logs,
        )

    def test_the_listener_opens_after_the_credentialed_server_holds_its_socket(self):
        # The order is the guarantee: whatever port the metrics listener is
        # given, a collision then costs the metrics and never the commands.
        order = []

        class _Recording(credential_proxy.ThreadingUnixHTTPServer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                order.append("credentialed")

        started = mock.MagicMock(side_effect=lambda *args: order.append("metrics"))
        self._serve(8766, started=started, unix_server=_Recording)
        self.assertEqual(["credentialed", "metrics"], order)

    def test_the_credentialed_port_is_refused_by_name_and_never_bound(self):
        # The harness serves on the socket, where Envoy holds port 8765 in the
        # shipped layout; the refusal has to hold there too.
        args_port = 8765
        refusal = credential_proxy._metrics_port_refusal(args_port, types.SimpleNamespace(port=args_port, unix_socket="/run/backend.sock"))
        self.assertIn("8765", refusal)
        self.assertIn("credentialed", refusal)
        self.assertIsNone(credential_proxy._metrics_port_refusal(8766, types.SimpleNamespace(port=args_port, unix_socket="")))
        self.assertIn("credentialed", credential_proxy._metrics_port_refusal(args_port, types.SimpleNamespace(port=args_port, unix_socket="")))
        self.assertIn("1-65535", credential_proxy._metrics_port_refusal(70000, types.SimpleNamespace(port=args_port, unix_socket="")))

    def test_a_zero_or_refused_port_is_reported_as_what_it_is(self):
        # Three ways to arrive at 0, three different things an operator
        # should read: unset, set to 0, or refused above as no integer.
        for env_value, expected in ((None, "is unset"), ("0", "'0'"), ("8766a", "'8766a'")):
            with self.subTest(env_value=env_value):
                started, logs = self._serve(0, env_value)
                started.assert_not_called()
                self.assertTrue(any("metrics listener disabled" in line and expected in line for line in logs), logs)
                if env_value is not None:
                    self.assertFalse(any("is unset" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
