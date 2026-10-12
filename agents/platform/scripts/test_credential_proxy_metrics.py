"""The credential broker's Prometheus surface.

A metrics-only listener serves the brokered tool invocations by tool,
subcommand and outcome; their wall-clock latency; the credentialed listener's
requests by route family and status code; the admission queue, as a histogram
of waits and a counter of refusals by the bound that held the request; and the
slots and child memory in use beside their caps. Two properties carry the
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
        CredentialProxyHandler.metrics = ProxyMetrics()
        CredentialProxyHandler.executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=4096,
            state_dir=str(Path(self.temp_dir.name) / "state"),
            scoped_pool=None,
            metrics=CredentialProxyHandler.metrics,
        )
        CredentialProxyHandler.metrics.set_admission_gauges(CredentialProxyHandler.executor.admission_snapshot)
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


def _hold_a_slot(case, executor, seconds):
    """Hold a request slot for `seconds` on another thread; return once held."""
    held = threading.Event()

    def hold():
        with executor.request_slot():
            held.set()
            time.sleep(seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    case.addCleanup(thread.join)
    case.assertTrue(held.wait(5), "the slot holder never got its slot")
    return thread


def _budgeted_executor(case, admits, metrics, max_output_bytes=1024):
    """An executor whose child memory budget admits exactly `admits` slot-taking requests."""
    per_request = (
        credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES
        + credential_proxy.OUTPUT_COPIES_PER_COMMAND * max_output_bytes
    )
    limit = (
        credential_proxy.BROKER_RESIDENT_RESERVE_BYTES
        + credential_proxy.CONTENT_WORKSPACE_RESERVE_BYTES
        + admits * per_request
    )
    floor = min(admits, credential_proxy.BUDGET_MINIMUM_ADMITTED_REQUESTS)
    with mock.patch.object(credential_proxy, "BUDGET_MINIMUM_ADMITTED_REQUESTS", floor):
        executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=max_output_bytes,
            state_dir=case.temp_dir.name,
            scoped_pool=None,
            max_concurrent_commands=8,
            memory_limit_bytes=limit,
            metrics=metrics,
        )
    case.assertEqual(admits, executor.requests_the_budget_admits())
    return executor


class AdmissionMetricsTest(unittest.TestCase):
    """The queue the child memory budget created, made visible: a histogram of
    admission waits by the bound that held the request, a counter of refusals
    by bound, and gauges of what is in use against each cap. Observed on the
    executor, rendered by the registry serve() hands it."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.metrics = ProxyMetrics()

    def executor(self, **kwargs):
        executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=1024,
            state_dir=self.temp_dir.name,
            scoped_pool=None,
            metrics=self.metrics,
            **kwargs,
        )
        self.metrics.set_admission_gauges(executor.admission_snapshot)
        return executor

    def families(self):
        return _parse(self.metrics.render())

    def test_every_admission_is_observed_even_a_short_one(self):
        # The log line starts at COMMAND_SLOT_WAIT_LOG_MS; the histogram does
        # not, so an idle broker's p50 is a measured zero, not an absence. An
        # admission that never waited blames neither bound: `none`.
        executor = self.executor()
        with executor.request_slot():
            pass
        families = self.families()
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="none"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_wait_seconds_bucket", bound="none", le="0.1"))
        self.assertIsNone(_series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="slot"))

    def test_an_unwaited_admission_is_none_whether_the_budget_is_on_or_off(self):
        # The label an idle broker shows does not flip with the memory limit:
        # at the 2Gi default most admissions wait for nothing, and `budget`
        # there would blame a budget that held nothing.
        for executor in (self.executor(), _budgeted_executor(self, admits=4, metrics=self.metrics)):
            with executor.request_slot():
                pass
        families = self.families()
        self.assertEqual(2, _series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="none"))
        self.assertIsNone(_series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="budget"))
        self.assertIsNone(_series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="slot"))

    def test_a_wait_for_a_slot_is_observed_under_the_slot_bound(self):
        executor = self.executor(max_concurrent_commands=1)
        _hold_a_slot(self, executor, seconds=1)
        queued_at = time.monotonic()
        with executor.request_slot():
            waited = time.monotonic() - queued_at
        families = self.families()
        # Two admissions: the holder's, which waited for nothing and is `none`,
        # and this one, under the slot cap.
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="none"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="slot"))
        self.assertIsNone(_series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="budget"))
        total = _series(families, "kubeagents_credential_proxy_admission_wait_seconds_sum", bound="slot")
        self.assertGreaterEqual(total, 0.5)
        self.assertLessEqual(total, waited + 0.1)
        # Cumulative buckets in bound order: this wait is past the first edge
        # and inside the last; the holder's zero sits in `none`'s first bucket.
        buckets = [
            _series(families, "kubeagents_credential_proxy_admission_wait_seconds_bucket", bound="slot", le=str(bound))
            for bound in credential_proxy.ADMISSION_WAIT_BUCKETS
        ]
        self.assertEqual(buckets, sorted(buckets))
        self.assertEqual(0, buckets[0])
        self.assertEqual(1, buckets[-1])
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_wait_seconds_bucket", bound="none", le="0.1"))

    def test_a_wait_for_the_budget_is_observed_under_the_budget_bound(self):
        # Eight slots, a budget for one: the second request waits for the
        # first's reservation, which is the queue #2632 measured.
        executor = _budgeted_executor(self, admits=1, metrics=self.metrics)
        self.metrics.set_admission_gauges(executor.admission_snapshot)
        _hold_a_slot(self, executor, seconds=1)
        with executor.request_slot():
            pass
        families = self.families()
        # The holder's admission waited for nothing (`none`); this one waited
        # under the budget: with the budget on and no slot ever full, the
        # budget is what the queue is for.
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="none"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="budget"))
        self.assertIsNone(_series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="slot"))
        self.assertGreaterEqual(_series(families, "kubeagents_credential_proxy_admission_wait_seconds_sum", bound="budget"), 0.5)

    def test_the_buckets_put_the_preflight_cap_and_the_refusal_bound_on_an_edge(self):
        # 15 s is the cap the Cluster Agent preflight puts on one brokered
        # call; 60 s is COMMAND_SLOT_WAIT_SECONDS, past which the
        # broker refuses. A dashboard reads "how often did a wait cross
        # either" off a bucket ratio only if both are bucket bounds.
        self.assertIn(15.0, credential_proxy.ADMISSION_WAIT_BUCKETS)
        self.assertIn(float(credential_proxy.COMMAND_SLOT_WAIT_SECONDS), credential_proxy.ADMISSION_WAIT_BUCKETS)
        self.assertEqual(list(credential_proxy.ADMISSION_WAIT_BUCKETS), sorted(credential_proxy.ADMISSION_WAIT_BUCKETS))

    def test_a_refusal_at_the_slot_cap_carries_its_bound(self):
        executor = self.executor(max_concurrent_commands=1)
        _hold_a_slot(self, executor, seconds=1)
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                with executor.request_slot():
                    self.fail("a slot was granted while the holder still had it")
        self.assertEqual(credential_proxy.ADMISSION_BOUND_SLOT, raised.exception.bound)

    def test_a_refusal_by_the_budget_carries_its_bound(self):
        executor = _budgeted_executor(self, admits=1, metrics=self.metrics)
        _hold_a_slot(self, executor, seconds=1)
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                with executor.request_slot():
                    self.fail("admitted past the budget")
        self.assertEqual(credential_proxy.ADMISSION_BOUND_BUDGET, raised.exception.bound)

    def test_with_the_budget_off_a_refusal_behind_the_queue_head_is_the_slot_cap(self):
        # The request fits and no slot is full at the instant of refusal, so
        # the queue ahead is what held it. With the budget off that queue can
        # only be waiting for a slot; `budget` here would send the operator to
        # raise a limit the broker is not using. The text agrees.
        executor = self.executor(max_concurrent_commands=2, memory_limit_bytes=None)
        with executor._slot_condition:
            executor._slots_in_use = 1
            bound = executor._held_bound(takes_slot=True, saw_slots_full=False)
            self.assertEqual(credential_proxy.ADMISSION_BOUND_SLOT, bound)
            self.assertIn("waiting on its limit of 2 concurrent commands", executor._refusal_text(True, bound))
            executor._slots_in_use = 0

    def test_with_the_budget_on_a_refusal_behind_the_queue_head_is_the_budget(self):
        executor = _budgeted_executor(self, admits=2, metrics=self.metrics)
        with executor._slot_condition:
            executor._slots_in_use = 1
            executor._reserved_bytes = credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES
            bound = executor._held_bound(takes_slot=True, saw_slots_full=False)
            self.assertEqual(credential_proxy.ADMISSION_BOUND_BUDGET, bound)
            self.assertIn("waiting for its child memory budget", executor._refusal_text(True, bound))
            executor._slots_in_use = 0
            executor._reserved_bytes = 0

    def test_a_refusal_in_the_wake_of_a_freed_slot_counts_under_the_cap_it_waited_behind_and_says_so(self):
        # Budget admitting more than the slot cap, so the cap is the binding
        # bound. At the instant of refusal a slot has just freed (the waiter
        # woke on that notify) and the request fits, yet it spent its wait
        # behind the cap: the counter and the 503 text must both say so, as
        # the histogram would.
        executor = _budgeted_executor(self, admits=4, metrics=self.metrics)
        executor.max_concurrent_commands = 2
        with executor._slot_condition:
            executor._slots_in_use = 1
            executor._reserved_bytes = credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES
            bound = executor._held_bound(takes_slot=True, saw_slots_full=True)
            self.assertEqual(credential_proxy.ADMISSION_BOUND_SLOT, bound)
            self.assertIn("concurrent commands", executor._refusal_text(True, bound))
            self.assertNotIn("memory budget", executor._refusal_text(True, bound))
            self.assertEqual(credential_proxy.ADMISSION_BOUND_BUDGET, executor._held_bound(takes_slot=True, saw_slots_full=False))
            executor._slots_in_use = 0
            executor._reserved_bytes = 0

    def test_a_budget_sized_to_the_slot_cap_counts_a_convoy_refusal_under_the_cap_like_its_waits(self):
        # Both bounds bind at once: two slots, a budget for exactly two, both
        # held. The ninth-of-eight shape on an install whose limit admits the
        # cap exactly. The waits in that convoy are observed under `slot`
        # (the cap was full); a refusal must count the same way, not under
        # `budget` because the budget also happens to be full at the instant.
        executor = _budgeted_executor(self, admits=2, metrics=self.metrics)
        executor.max_concurrent_commands = 2
        with executor._slot_condition:
            executor._slots_in_use = 2
            executor._reserved_bytes = 2 * credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES
            self.assertFalse(executor._fits_budget(True), "the budget is full too")
            bound = executor._held_bound(takes_slot=True, saw_slots_full=True)
            self.assertEqual(credential_proxy.ADMISSION_BOUND_SLOT, bound)
            self.assertIn("without reaching a free slot", executor._refusal_text(True, bound))
            executor._slots_in_use = 0
            executor._reserved_bytes = 0

    def test_a_slot_less_reserver_is_held_by_the_budget_whatever_the_cap_did(self):
        executor = _budgeted_executor(self, admits=2, metrics=self.metrics)
        self.assertEqual(credential_proxy.ADMISSION_BOUND_BUDGET, executor._held_bound(takes_slot=False, saw_slots_full=True))

    def test_a_reservation_that_yielded_is_observed_once_from_its_first_arrival(self):
        # A route refresher that steps aside for a vcs verb leaves `_admit` by
        # AdmissionYielded, which carries its arrival, and re-enters with a
        # deadline on the same thread handing the arrival back: one
        # admission, observed once, from the first leg's arrival.
        executor = _budgeted_executor(self, admits=1, metrics=self.metrics)
        holder = _hold_a_slot(self, executor, 0.4)  # the one admission the budget has
        with self.assertRaises(credential_proxy.AdmissionYielded) as raised:
            with executor.reserve_child_memory(yield_when=lambda: True):
                self.fail("admitted instead of yielding")
        arrival = raised.exception.queued_at
        # Only the holder's own (unwaited, `none`) admission is observed so far.
        self.assertIsNone(_series(_parse(self.metrics.render()), "kubeagents_credential_proxy_admission_wait_seconds_count", bound="budget"))
        holder.join()
        with executor.reserve_child_memory(deadline=time.monotonic() + 5, queued_at=arrival):
            pass
        families = _parse(self.metrics.render())
        # The second leg was admitted at once, but it carries the first leg's
        # arrival, so it is a waited admission under the budget, not `none`.
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="budget"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_wait_seconds_count", bound="none"))
        self.assertGreaterEqual(_series(families, "kubeagents_credential_proxy_admission_wait_seconds_sum", bound="budget"), 0.4)

    def test_the_snapshot_is_typed_and_the_render_reads_its_fields(self):
        snapshot = self.executor(max_concurrent_commands=3).admission_snapshot()
        self.assertIsInstance(snapshot, credential_proxy.AdmissionSnapshot)
        self.assertEqual((0, 3, 0, None), tuple(snapshot))

    def test_a_refusal_at_the_refresh_lock_carries_its_bound(self):
        executor = self.executor()
        self.assertTrue(executor._refresh_lock.acquire(blocking=False))
        self.addCleanup(executor._refresh_lock.release)
        with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
            executor._acquire_refresh_lock("github", time.monotonic() + 0.1, None)
        self.assertEqual(credential_proxy.ADMISSION_BOUND_REFRESH_LOCK, raised.exception.bound)

    def test_a_refusal_by_the_session_limit_carries_its_bound(self):
        slots = credential_proxy.SessionSlots(1)
        with slots.acquire(credential_proxy.CALLER_ROLE_SESSION):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                with slots.acquire(credential_proxy.CALLER_ROLE_SESSION):
                    self.fail("a second session command was admitted past the limit")
        self.assertEqual(credential_proxy.ADMISSION_BOUND_SESSION, raised.exception.bound)

    def test_refusals_are_counted_by_bound_and_nothing_else(self):
        self.metrics.record_refusal(credential_proxy.ADMISSION_BOUND_SLOT)
        self.metrics.record_refusal(credential_proxy.ADMISSION_BOUND_SLOT)
        self.metrics.record_refusal(credential_proxy.ADMISSION_BOUND_BUDGET)
        families = self.families()
        self.assertEqual(2, _series(families, "kubeagents_credential_proxy_admission_refusals_total", bound="slot"))
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_refusals_total", bound="budget"))
        for labels in families["kubeagents_credential_proxy_admission_refusals_total"]:
            self.assertEqual({"bound"}, {key for key, _ in labels})
            self.assertRegex(dict(labels)["bound"], _WORD_LABEL)

    def test_a_refusal_bound_outside_the_vocabulary_is_counted_as_other(self):
        # The label set is closed: a bound the code does not name counts under
        # `other` rather than opening a series per message.
        self.metrics.record_refusal("something a caller wrote")
        families = self.families()
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_refusals_total", bound="other"))
        self.assertNotIn("something a caller wrote", self.metrics.render())

    def test_the_gauges_read_the_slots_and_bytes_in_use_against_each_cap(self):
        executor = _budgeted_executor(self, admits=2, metrics=self.metrics)
        self.metrics.set_admission_gauges(executor.admission_snapshot)
        idle = self.families()
        self.assertEqual(0, _series(idle, "kubeagents_credential_proxy_slots_in_use"))
        self.assertEqual(0, _series(idle, "kubeagents_credential_proxy_child_memory_reserved_bytes"))
        self.assertEqual(8, _series(idle, "kubeagents_credential_proxy_slot_cap"))
        self.assertEqual(executor.children_budget_bytes, _series(idle, "kubeagents_credential_proxy_child_memory_budget_bytes"))
        _hold_a_slot(self, executor, seconds=1)
        busy = self.families()
        self.assertEqual(1, _series(busy, "kubeagents_credential_proxy_slots_in_use"))
        self.assertEqual(
            credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES,
            _series(busy, "kubeagents_credential_proxy_child_memory_reserved_bytes"),
        )

    def test_with_the_budget_off_the_budget_gauge_is_absent_and_the_cap_stands(self):
        # No limit at all, or one under the floor: the budget is off and the
        # series is absent, which is the signal #2463 describes, not a zero
        # that reads as "nothing reserved".
        self.executor(memory_limit_bytes=None)
        families = self.families()
        self.assertNotIn("kubeagents_credential_proxy_child_memory_budget_bytes", families)
        self.assertEqual(credential_proxy.DEFAULT_MAX_CONCURRENT_COMMANDS, _series(families, "kubeagents_credential_proxy_slot_cap"))

    def test_a_registry_without_an_executor_renders_no_gauges(self):
        families = self.families()
        for name in (
            "kubeagents_credential_proxy_slots_in_use",
            "kubeagents_credential_proxy_child_memory_reserved_bytes",
            "kubeagents_credential_proxy_slot_cap",
            "kubeagents_credential_proxy_child_memory_budget_bytes",
        ):
            self.assertNotIn(name, families)


class BusyRouteCountingTest(_BrokerFixture):
    def test_a_busy_exec_answer_counts_a_refusal_under_its_bound(self):
        # The one site per route that answers the busy 503 counts the refusal
        # from the exception's bound, so the counter never parses a message.
        executor = CredentialProxyHandler.executor
        executor.max_concurrent_commands = 1
        _hold_a_slot(self, executor, seconds=2)
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            status, body = self.post(["kubectl", "get", "pods"])
        self.assertEqual((503, "busy", "CREDENTIAL_PROXY_BUSY"), (status, body["status"], body["code"]))
        families = self.families()
        self.assertEqual(1, _series(families, "kubeagents_credential_proxy_admission_refusals_total", bound="slot"))
        self.assertEqual(1, _series(families, "kubeagents_tool_invocations_total", tool="kubectl", subcommand="get", status="busy"))


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

    def test_the_process_start_time_is_exported_once_and_never_moves(self):
        """The gauge the operator's usage poller reads: a plausible time, typed, constant across scrapes."""
        first = _parse(urllib.request.urlopen(self.endpoint + "/metrics").read().decode("utf-8"))
        second = _parse(urllib.request.urlopen(self.endpoint + "/metrics").read().decode("utf-8"))
        start = _series(first, credential_proxy.PROCESS_START_TIME_METRIC)
        self.assertIsNotNone(start, "no process_start_time_seconds line")
        self.assertEqual(start, credential_proxy.PROCESS_START_TIME_SECONDS)
        self.assertEqual(start, _series(second, credential_proxy.PROCESS_START_TIME_METRIC))
        self.assertLess(abs(time.time() - start), 24 * 3600, "the start time is not this process's")
        body = urllib.request.urlopen(self.endpoint + "/metrics").read().decode("utf-8")
        self.assertIn(f"# TYPE {credential_proxy.PROCESS_START_TIME_METRIC} gauge", body)

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
        status, body = self._post_with(scoped_sa_pool.PoolRefusal("no scoped service account for project p (cluster projects/p/locations/l/clusters/c): refused; the broker will not fall back to the ambient credential. Declare the project in spec.scope and apply, or exclude the cluster."))
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
