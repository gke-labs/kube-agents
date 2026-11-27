"""Tests for credential_proxy active-standby warm-up, bearer control, and drain handoff."""

from __future__ import annotations

import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import credential_proxy
from credential_proxy import (
    BROKER_STATE_ACTIVE,
    BROKER_STATE_DRAINED,
    BROKER_STATE_DRAINING,
    BROKER_STATE_STANDBY,
    BrokerLifecycle,
    CredentialProxyHandler,
    HealthHandler,
    HealthServer,
    MetricsHandler,
    MetricsServer,
    Policy,
    ProxyMetrics,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    port: int,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, object] | str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        req_headers = dict(headers or {})
        if body:
            req_headers["Content-Length"] = str(len(body))
        conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, raw
    finally:
        conn.close()


class _FakeRelay:
    def __init__(self, unsettled: int = 0) -> None:
        self._unsettled = unsettled
        self.released = 0
        self.lock = threading.Lock()

    def has_unsettled(self) -> bool:
        with self.lock:
            return self._unsettled > 0

    def release_unsettled(self) -> int:
        with self.lock:
            count = self._unsettled
            self._unsettled = 0
            self.released += count
            return count

    def settle_all(self) -> None:
        with self.lock:
            self._unsettled = 0


class ActiveStandbyLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.token_path = Path(self._tmp.name) / "control-token"
        self.token_value = "test-control-secret-value-0123456789"
        self.token_path.write_text(self.token_value, encoding="utf-8")

        self._saved = {}
        for attr in (
            "lifecycle",
            "metrics",
            "broker_role",
            "slack_relay",
            "readings_sweep",
            "policy",
            "enforce_read_only",
            "profile_task_writes",
            "authenticator",
            "vcs",
            "profile_gate",
            "max_request_bytes",
            "a2a_chat_relay",
        ):
            self._saved[attr] = (attr in CredentialProxyHandler.__dict__, CredentialProxyHandler.__dict__.get(attr))
        self.addCleanup(self._restore_handler_attrs)

        CredentialProxyHandler.draining.clear()
        CredentialProxyHandler.handoff_draining.clear()
        CredentialProxyHandler.shutting_down.clear()
        CredentialProxyHandler.slack_relay = None
        CredentialProxyHandler.readings_sweep = None
        CredentialProxyHandler.metrics = ProxyMetrics()
        CredentialProxyHandler.profile_task_writes = credential_proxy.ProfileTaskWrites()
        CredentialProxyHandler.broker_role = credential_proxy.ROLE_BROKER

    def _restore_handler_attrs(self) -> None:
        CredentialProxyHandler.draining.clear()
        CredentialProxyHandler.handoff_draining.clear()
        CredentialProxyHandler.shutting_down.clear()
        for attr, (existed, value) in self._saved.items():
            if existed:
                setattr(CredentialProxyHandler, attr, value)
            elif hasattr(CredentialProxyHandler, attr):
                delattr(CredentialProxyHandler, attr)

    def test_standby_activates_and_drains_over_live_http_listeners(self) -> None:
        activated = []
        fake_slack = mock.Mock()
        fake_slack.undelivered.return_value = 0
        fake_sweep = mock.Mock()
        fake_relay = _FakeRelay(unsettled=0)

        lifecycle = BrokerLifecycle(
            enabled=True,
            control_file=self.token_path,
            relays=[fake_relay],
        )
        CredentialProxyHandler.lifecycle = lifecycle
        lifecycle.set_warm(
            on_activate=lambda: (
                activated.append(True),
                setattr(CredentialProxyHandler, "slack_relay", fake_slack),
                setattr(CredentialProxyHandler, "readings_sweep", fake_sweep),
            )
        )

        proxy_server = credential_proxy.ThreadingTCPHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        metrics_server = MetricsServer(("127.0.0.1", 0), MetricsHandler)
        health_server = HealthServer(("127.0.0.1", 0), HealthHandler)
        for srv in (proxy_server, metrics_server, health_server):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.shutdown)
            self.addCleanup(srv.server_close)

        proxy_port = proxy_server.server_address[1]
        metrics_port = metrics_server.server_address[1]
        health_port = health_server.server_address[1]

        # Standby: health listeners report 503 standby, relays are not started yet.
        self.assertEqual((503, credential_proxy.HEALTHZ_STANDBY_PAYLOAD), _request(proxy_port, "GET", "/healthz"))
        self.assertEqual((503, credential_proxy.HEALTHZ_STANDBY_PAYLOAD), _request(health_port, "GET", "/healthz"))
        self.assertEqual([], activated)

        # Control endpoints enforce bearer authentication while /metrics remains open and unrelated routes return 404.
        status, _ = _request(metrics_port, "GET", "/control/status")
        self.assertEqual(HTTPStatus.UNAUTHORIZED, status)
        status, _ = _request(metrics_port, "POST", "/control/activate", {"Authorization": "Bearer wrong"})
        self.assertEqual(HTTPStatus.UNAUTHORIZED, status)
        status, _ = _request(metrics_port, "GET", "/healthz")
        self.assertEqual(HTTPStatus.NOT_FOUND, status)
        status, _ = _request(health_port, "POST", "/control/activate", {"Authorization": f"Bearer {self.token_value}"})
        self.assertEqual(HTTPStatus.NOT_FOUND, status)
        status, _ = _request(health_port, "POST", "/control/drain", {"Authorization": f"Bearer {self.token_value}"})
        self.assertEqual(HTTPStatus.NOT_FOUND, status)
        status, body = _request(metrics_port, "GET", "/metrics")
        self.assertEqual(HTTPStatus.OK, status)
        self.assertIn(credential_proxy.PROCESS_START_TIME_METRIC, str(body))

        auth = {"Authorization": f"Bearer {self.token_value}"}
        status, snap = _request(metrics_port, "GET", "/control/status", auth)
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(BROKER_STATE_STANDBY, snap["state"])
        self.assertTrue(snap["warm"])

        # Activation runs deferred callback once, flips /healthz to 200, and is idempotent.
        self.assertEqual((200, BROKER_STATE_ACTIVE), (_request(metrics_port, "POST", "/control/activate", auth)[0], _request(metrics_port, "POST", "/control/activate", auth)[1]["state"]))
        self.assertEqual(1, len(activated))
        self.assertEqual((200, credential_proxy.HEALTHZ_PAYLOAD), _request(proxy_port, "GET", "/healthz"))
        self.assertEqual((200, credential_proxy.HEALTHZ_PAYLOAD), _request(health_port, "GET", "/healthz"))

        # Idle drain immediately moves to drained, stops intake, rejects re-activation,
        # and keeps /healthz 200 until SIGTERM sets shutting_down (preventing empty
        # Service Endpoints before the replacement pod is activated and Ready).
        status, drain_resp = _request(metrics_port, "POST", "/control/drain", auth)
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual(BROKER_STATE_DRAINED, drain_resp["state"])
        fake_slack.stop_intake.assert_called_once_with()
        fake_sweep.stop.assert_called_once_with()
        self.assertEqual((200, credential_proxy.HEALTHZ_PAYLOAD), _request(proxy_port, "GET", "/healthz"))
        self.assertEqual((200, credential_proxy.HEALTHZ_PAYLOAD), _request(health_port, "GET", "/healthz"))
        conflict_status, _ = _request(metrics_port, "POST", "/control/activate", auth)
        self.assertEqual(HTTPStatus.CONFLICT, conflict_status)

        CredentialProxyHandler.shutting_down.set()
        self.assertEqual((503, credential_proxy.HEALTHZ_DRAINING_PAYLOAD), _request(proxy_port, "GET", "/healthz"))
        self.assertEqual((503, credential_proxy.HEALTHZ_DRAINING_PAYLOAD), _request(health_port, "GET", "/healthz"))

    def test_handoff_draining_and_drained_refusals_set_expected_retry_after_headers(self) -> None:
        self.assertEqual(30.0, credential_proxy.HANDOFF_DRAIN_SECONDS)
        self.assertEqual(18, credential_proxy.HANDOFF_DRAINING_RETRY_AFTER_SECONDS)
        self.assertEqual(5, credential_proxy.HANDOFF_DRAINED_RETRY_AFTER_SECONDS)
        self.assertEqual(45.0, credential_proxy.STANDBY_SELF_ACTIVATE_GRACE_SECONDS)
        fake_relay = _FakeRelay(unsettled=1)
        lifecycle = BrokerLifecycle(
            enabled=True,
            control_file=self.token_path,
            relays=[fake_relay],
            settle_seconds=0.5,
            drain_seconds=1.0,
            end_wait_seconds=0.1,
        )
        lifecycle.set_warm()
        lifecycle.activate()
        CredentialProxyHandler.lifecycle = lifecycle
        CredentialProxyHandler.draining.set()

        seen_headers = []

        class _FakeBusyHandler(CredentialProxyHandler):
            def __init__(self) -> None:
                self.metrics = ProxyMetrics()

            def _json(self, status, payload, headers=None):
                seen_headers.append((lifecycle.state, status, payload.get("bound"), headers))

        handler = _FakeBusyHandler()
        lifecycle.drain()
        self.assertEqual(BROKER_STATE_DRAINING, lifecycle.state)
        handler._busy(credential_proxy.CommandSlotUnavailable("draining", bound=credential_proxy.ADMISSION_BOUND_DRAINING), before_admission=True)

        fake_relay.settle_all()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and lifecycle.state != BROKER_STATE_DRAINED:
            time.sleep(0.02)
        self.assertEqual(BROKER_STATE_DRAINED, lifecycle.state)
        handler._busy(credential_proxy.CommandSlotUnavailable("draining", bound=credential_proxy.ADMISSION_BOUND_DRAINING), before_admission=True)

        self.assertEqual(
            [
                (BROKER_STATE_DRAINING, HTTPStatus.SERVICE_UNAVAILABLE, "draining", {"Retry-After": str(credential_proxy.HANDOFF_DRAINING_RETRY_AFTER_SECONDS)}),
                (BROKER_STATE_DRAINED, HTTPStatus.SERVICE_UNAVAILABLE, "draining", {"Retry-After": str(credential_proxy.HANDOFF_DRAINED_RETRY_AFTER_SECONDS)}),
            ],
            seen_headers,
        )

    def test_drain_waits_for_in_flight_and_unsettled_chat_before_marking_drained(self) -> None:
        fake_relay = _FakeRelay(unsettled=2)
        lifecycle = BrokerLifecycle(
            enabled=True,
            control_file=self.token_path,
            relays=[fake_relay],
            settle_seconds=0.3,
            drain_seconds=1.0,
            end_wait_seconds=0.2,
        )
        CredentialProxyHandler.lifecycle = lifecycle
        lifecycle.set_warm()
        status, _ = lifecycle.activate()
        self.assertEqual(HTTPStatus.OK, status)

        CredentialProxyHandler.in_flight.enter()
        try:
            drain_status, snap = lifecycle.drain()
            self.assertEqual(HTTPStatus.OK, drain_status)
            self.assertEqual(BROKER_STATE_DRAINING, snap["state"])
            self.assertEqual(BROKER_STATE_DRAINING, lifecycle.state)
            # While BROKER_STATE_DRAINING before SIGTERM, /healthz stays 200 ok so Service
            # Endpoints remain routed to this pod for DRAIN_OPEN_ROUTES / DRAIN_OPEN_REPLY_ROUTES.
            self.assertEqual((HTTPStatus.OK, credential_proxy.HEALTHZ_PAYLOAD), credential_proxy.healthz_response())
        finally:
            CredentialProxyHandler.in_flight.leave()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and lifecycle.state != BROKER_STATE_DRAINED:
            time.sleep(0.02)
        self.assertEqual(BROKER_STATE_DRAINED, lifecycle.state)
        self.assertEqual(2, fake_relay.released)
        self.assertEqual((HTTPStatus.OK, credential_proxy.HEALTHZ_PAYLOAD), credential_proxy.healthz_response())
        CredentialProxyHandler.shutting_down.set()
        self.assertEqual((HTTPStatus.SERVICE_UNAVAILABLE, credential_proxy.HEALTHZ_DRAINING_PAYLOAD), credential_proxy.healthz_response())

    def test_drain_waits_for_undelivered_in_memory_relays_and_sigterm_releases_late_chat_receipts(self) -> None:
        class _FakeInMemoryRelay:
            def __init__(self, count: int) -> None:
                self._count = count
                self.stopped = False

            def stop_intake(self) -> None:
                self.stopped = True

            def undelivered(self) -> int:
                return self._count

        fake_slack = _FakeInMemoryRelay(count=1)
        fake_chat = _FakeRelay(unsettled=0)
        CredentialProxyHandler.slack_relay = fake_slack
        lifecycle = BrokerLifecycle(
            enabled=True,
            control_file=self.token_path,
            relays=[fake_chat],
            settle_seconds=0.3,
            drain_seconds=0.5,
            end_wait_seconds=0.1,
        )
        CredentialProxyHandler.lifecycle = lifecycle
        lifecycle.set_warm()
        status, _ = lifecycle.activate()
        self.assertEqual(HTTPStatus.OK, status)

        # Undelivered Slack events hold drain in BROKER_STATE_DRAINING rather than marking drained at 0ms.
        drain_status, snap = lifecycle.drain()
        self.assertEqual(HTTPStatus.OK, drain_status)
        self.assertEqual(BROKER_STATE_DRAINING, snap["state"])
        self.assertTrue(fake_slack.stopped)

        fake_slack._count = 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and lifecycle.state != BROKER_STATE_DRAINED:
            time.sleep(0.02)
        self.assertEqual(BROKER_STATE_DRAINED, lifecycle.state)

        # Late Pub/Sub Chat receipt pulled on an open long poll after BROKER_STATE_DRAINED
        # is still released when SIGTERM triggers shutdown.
        fake_chat._unsettled = 1
        fake_server = mock.Mock()
        handlers = {}
        orig_signal = credential_proxy.signal.getsignal(credential_proxy.signal.SIGTERM)
        self.addCleanup(credential_proxy.signal.signal, credential_proxy.signal.SIGTERM, orig_signal)
        with mock.patch.object(credential_proxy.signal, "signal", side_effect=lambda sig, fn: handlers.setdefault(sig, fn)):
            credential_proxy.install_shutdown_handler(fake_server, [fake_chat], settle_seconds=5.0, drain_seconds=5.0)
        sigterm = handlers[credential_proxy.signal.SIGTERM]
        sigterm(credential_proxy.signal.SIGTERM, None)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not fake_server.shutdown.called:
            time.sleep(0.02)
        fake_server.shutdown.assert_called_once_with()
        self.assertEqual(1, fake_chat.released)

    def test_profile_task_writes_export_import_filters_unconfirmed_expired_and_caps_entries(self) -> None:
        now = 1_700_000_000.0
        src = credential_proxy.ProfileTaskWrites(clock=lambda: now, max_entries=10)
        # Confirmed branch + proposal stay live; expired entries and unconfirmed pending claims do not export.
        src._branches[("github:/acme/infra", "platform-agent/pending")] = credential_proxy._TaskOwned(
            "task-1", now + 600.0, confirmed=False
        )
        src._branches[("github:/acme/infra", "platform-agent/expired")] = credential_proxy._TaskOwned(
            "task-1", now - 10.0, confirmed=True
        )
        ok, fresh = src.claim_branch("task-1", now + 600.0, "github:/acme/infra", "platform-agent/live", lambda: False)
        self.assertTrue((ok, fresh) == (True, True))
        src.record_proposal("task-1", now + 600.0, "github:/acme/infra", 42)
        src.record_proposal("task-1", now - 5.0, "github:/acme/infra", 99)

        exported = src.export_state()
        self.assertEqual(
            [{"repository": "github:/acme/infra", "branch": "platform-agent/live", "task": "task-1", "expires_at": now + 600.0}],
            exported["branches"],
        )
        self.assertEqual(
            [{"repository": "github:/acme/infra", "number": 42, "task": "task-1", "expires_at": now + 600.0}],
            exported["proposals"],
        )

        # Target honors max_entries without evicting live entries and drops expired/invalid records.
        capped = credential_proxy.ProfileTaskWrites(clock=lambda: now, max_entries=2)
        capped.record_proposal("task-existing", now + 600.0, "github:/acme/infra", 1)
        capped.import_state({
            "branches": [
                {"repository": "github:/acme/infra", "branch": "platform-agent/live", "task": "task-1", "expires_at": now + 600.0},
                {"repository": "github:/acme/infra", "branch": "platform-agent/stale", "task": "task-1", "expires_at": now - 1.0},
            ],
            "proposals": [
                {"repository": "github:/acme/infra", "number": 42, "task": "task-1", "expires_at": now + 600.0},
            ],
        })
        self.assertTrue(capped.owns_proposal("task-existing", "github:/acme/infra", 1))
        self.assertTrue(capped.owns_branch("task-1", "github:/acme/infra", "platform-agent/live"))
        self.assertFalse(capped.owns_branch("task-1", "github:/acme/infra", "platform-agent/stale"))
        self.assertFalse(capped.owns_proposal("task-1", "github:/acme/infra", 42))
        atomic = credential_proxy.ProfileTaskWrites(clock=lambda: now, max_entries=10)
        with self.assertRaises(ValueError):
            atomic.import_state({
                "branches": [
                    {"repository": "github:/acme/infra", "branch": "platform-agent/first", "task": "task-1", "expires_at": now + 600.0},
                    {"repository": "r", "branch": "b", "task": "t", "expires_at": True},
                ]
            })
        self.assertFalse(atomic.owns_branch("task-1", "github:/acme/infra", "platform-agent/first"))
        for bad_payload in ({"branches": False}, {"proposals": 0}):
            with self.assertRaises(ValueError):
                atomic.import_state(bad_payload)

    def test_standby_refuses_vcs_and_active_standby_handoff_transfers_profile_task_writes(self) -> None:
        import contextlib
        import email.message
        import io
        import vcs_broker

        now = time.time()
        expires = now + 3600.0
        repo = "acme/infra"
        repo_url = f"https://github.com/{repo}"
        forge = SimpleNamespace(name="github", hosts=("github.com",))
        key = credential_proxy.task_repository_key(forge, repo)

        class _StaticAuth:
            authenticates = True
            def authenticate(self, headers):
                role = headers.get("X-Test-Role", credential_proxy.CALLER_ROLE_PROFILE)
                return credential_proxy.Principal(workload="w", uid="u", role=role, pod="p", pod_uid="u")

        class _StaticGate:
            def admit(self, principal, token):  # noqa: ARG002
                t = SimpleNamespace(task="task-1", expires=expires, permits=frozenset({credential_proxy.VCS_PROPOSAL_CREATE_PERMIT}), report_only=False)
                return credential_proxy.replace(principal, task=t)

        CredentialProxyHandler.authenticator = _StaticAuth()
        CredentialProxyHandler.profile_gate = _StaticGate()
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.a2a_chat_relay = SimpleNamespace(subscription_path="projects/p/subscriptions/s")

        # 1. Standby broker refuses POST /v1/vcs/publish (including >1 MiB bodies) over HTTP
        # with retryable 503 bound="draining", and defers /v1/chat/a2a/events so subscription survives.
        lifecycle = BrokerLifecycle(enabled=True, control_file=self.token_path)
        CredentialProxyHandler.lifecycle = lifecycle
        lifecycle.set_warm()
        proxy_server = credential_proxy.ThreadingTCPHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        metrics_server = MetricsServer(("127.0.0.1", 0), MetricsHandler)
        for srv in (proxy_server, metrics_server):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.shutdown)
            self.addCleanup(srv.server_close)

        status, resp = _request(
            proxy_server.server_address[1],
            "POST",
            "/v1/vcs/publish",
            body=json.dumps({"repository": repo_url, "branch": "platform-agent/standby-test"}).encode("utf-8"),
        )
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, status)
        self.assertEqual("CREDENTIAL_PROXY_BUSY", resp["code"])
        self.assertEqual("draining", resp["bound"])

        large_body = json.dumps({"repository": repo_url, "branch": "platform-agent/big", "bundle": "x" * ((1 << 20) + 4096)}).encode("utf-8")
        large_status, large_resp = _request(proxy_server.server_address[1], "POST", "/v1/vcs/publish", body=large_body)
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, large_status)
        self.assertEqual("CREDENTIAL_PROXY_BUSY", large_resp["code"])
        self.assertEqual("draining", large_resp["bound"])

        a2a_status, a2a_resp = _request(
            proxy_server.server_address[1],
            "GET",
            "/v1/chat/a2a/events",
            headers={"X-Test-Role": credential_proxy.CALLER_ROLE_A2A_CHAT},
        )
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, a2a_status)
        self.assertEqual("projects/p/subscriptions/s", a2a_resp.get("subscription"))

        # 2. Simulate an active predecessor holding task-1's claimed branch + opened proposal (and task-2's),
        # drain it over /control/drain, verify task_writes exports cleanly on drain and status,
        # and import into a fresh standby record via HTTP POST /control/activate.
        old_writes = credential_proxy.ProfileTaskWrites(clock=lambda: now)
        ok, _ = old_writes.claim_branch("task-1", expires, key, "platform-agent/t1-branch", lambda: False)
        self.assertTrue(ok)
        old_writes.record_proposal("task-1", expires, key, 17)
        old_writes.record_proposal("task-2", expires, key, 18)

        CredentialProxyHandler.profile_task_writes = old_writes
        old_lifecycle = BrokerLifecycle(enabled=True, control_file=self.token_path)
        old_lifecycle.set_warm()
        old_lifecycle.activate()
        drain_status, drain_snap = old_lifecycle.drain()
        self.assertEqual(HTTPStatus.OK, drain_status)
        self.assertEqual(BROKER_STATE_DRAINED, drain_snap["state"])
        self.assertIn("task_writes", drain_snap)
        self.assertIn("task_writes", old_lifecycle.snapshot())

        # Swap in fresh standby broker's empty record on the live metrics listener and activate with transferred state.
        fresh_writes = credential_proxy.ProfileTaskWrites(clock=lambda: now)
        CredentialProxyHandler.profile_task_writes = fresh_writes
        CredentialProxyHandler.lifecycle = lifecycle
        auth = {"Authorization": f"Bearer {self.token_value}", "Content-Type": "application/json"}
        act_status, act_snap = _request(
            metrics_server.server_address[1],
            "POST",
            "/control/activate",
            headers=auth,
            body=json.dumps({"task_writes": drain_snap["task_writes"]}).encode("utf-8"),
        )
        self.assertEqual(HTTPStatus.OK, act_status)
        self.assertEqual(BROKER_STATE_ACTIVE, act_snap["state"])

        # Verify task-1 keeps ownership of proposal #17 and branch on the activated pod, while task-2's #18 is refused.
        principal = credential_proxy.replace(
            credential_proxy.Principal(workload="w", uid="u", role=credential_proxy.CALLER_ROLE_PROFILE, pod="p", pod_uid="u"),
            task=SimpleNamespace(task="task-1", expires=expires, permits=frozenset({credential_proxy.VCS_PROPOSAL_CREATE_PERMIT})),
        )
        fake_vcs = SimpleNamespace(
            registry=SimpleNamespace(resolve=lambda named: (forge, repo)),
            snapshot_source=lambda named: (named, None),
            require_agent_proposal=lambda verb, payload: None,
            proposal_update=lambda payload: {"proposal": {"number": payload["number"]}},
            proposal_create=lambda payload: {"proposal": {"number": 99}},
        )

        def call_vcs(verb: str, fields: dict[str, object]) -> tuple[int, dict[str, object]]:
            raw = json.dumps({"repository": repo_url, **fields}).encode("utf-8")
            handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
            handler.principal = principal
            handler.vcs = fake_vcs
            handler.profile_task_writes = fresh_writes
            handler.headers = email.message.Message()
            handler.headers["Content-Length"] = str(len(raw))
            handler.rfile = io.BytesIO(raw)
            handler.max_request_bytes = 1 << 20
            handler._session_slot = lambda role: contextlib.nullcontext()
            handler._request_slot = lambda klass=None: contextlib.nullcontext()
            handler._repository_is_permitted = lambda repository, f: True
            replies: list[tuple[int, dict[str, object]]] = []
            handler._json = lambda st, pl: replies.append((int(st), pl))
            with mock.patch.object(vcs_broker, "route_table", return_value={
                "proposal-update": fake_vcs.proposal_update,
                "proposal-create": fake_vcs.proposal_create,
            }):
                handler._dispatch_vcs_post(verb)
            return replies[0]

        st_own, _ = call_vcs("proposal-update", {"number": 17, "title": "continued"})
        self.assertEqual(HTTPStatus.OK, st_own)
        st_create, _ = call_vcs("proposal-create", {"source": "platform-agent/t1-branch", "title": "t", "target": "main"})
        self.assertEqual(HTTPStatus.OK, st_create)
        st_other, pl_other = call_vcs("proposal-update", {"number": 18, "title": "stolen"})
        self.assertEqual(HTTPStatus.FORBIDDEN, st_other)
        self.assertEqual(credential_proxy.PROPOSAL_NOT_THIS_TASKS_CODE, pl_other["code"])

    def test_drain_reports_draining_while_in_flight_work_remains(self) -> None:
        CredentialProxyHandler.in_flight.enter()
        left = False
        try:
            lifecycle = BrokerLifecycle(
                enabled=True,
                control_file=self.token_path,
                drain_seconds=1.0,
                settle_seconds=0.05,
                end_wait_seconds=0.1,
            )
            lifecycle.set_warm()
            status, activate_snap = lifecycle.activate()
            self.assertEqual(HTTPStatus.OK, status)
            self.assertEqual(BROKER_STATE_ACTIVE, activate_snap["state"])

            status, drain_snap = lifecycle.drain()
            self.assertEqual(HTTPStatus.OK, status)
            self.assertEqual(BROKER_STATE_DRAINING, drain_snap["state"])
            self.assertEqual(1, drain_snap["in_flight"])
            self.assertEqual((HTTPStatus.CONFLICT, BROKER_STATE_DRAINING), (lifecycle.activate()[0], lifecycle.state))
        finally:
            if not left:
                CredentialProxyHandler.in_flight.leave()
                left = True

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and lifecycle.state != BROKER_STATE_DRAINED:
            time.sleep(0.02)
        self.assertEqual(BROKER_STATE_DRAINED, lifecycle.state)

    def test_standby_self_activates_when_no_active_peer_answers_200(self) -> None:
        activated: list[str] = []
        lifecycle = BrokerLifecycle(
            enabled=True,
            control_file=self.token_path,
            peer_active_probe=lambda: False,
            peer_probe_interval_seconds=0.01,
            self_activate_grace_seconds=0.04,
        )
        self.addCleanup(lifecycle.stop_watchdog)
        self.assertEqual(BROKER_STATE_STANDBY, lifecycle.state)
        lifecycle.set_warm(on_activate=lambda: activated.append("started"))

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and lifecycle.state != BROKER_STATE_ACTIVE:
            time.sleep(0.01)
        self.assertEqual(BROKER_STATE_ACTIVE, lifecycle.state)
        self.assertEqual(["started"], activated)

    def test_standby_waits_while_peer_healthz_is_200_then_recovers_if_peer_dies(self) -> None:
        peer_alive = threading.Event()
        peer_alive.set()
        peer_status_code = [HTTPStatus.OK]

        class _PeerHandler(credential_proxy.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                code = peer_status_code[0] if peer_alive.is_set() else HTTPStatus.SERVICE_UNAVAILABLE
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        peer_srv = credential_proxy.ThreadingTCPHTTPServer(("127.0.0.1", 0), _PeerHandler)
        threading.Thread(target=peer_srv.serve_forever, daemon=True).start()
        self.addCleanup(peer_srv.shutdown)
        self.addCleanup(peer_srv.server_close)
        peer_url = f"http://127.0.0.1:{peer_srv.server_address[1]}/healthz"

        activated: list[str] = []
        lifecycle = BrokerLifecycle(
            enabled=True,
            control_file=self.token_path,
            peer_health_url=peer_url,
            peer_probe_interval_seconds=0.01,
            self_activate_grace_seconds=0.05,
        )
        self.addCleanup(lifecycle.stop_watchdog)
        lifecycle.set_warm(on_activate=lambda: activated.append("recovered"))

        time.sleep(0.12)
        self.assertEqual(BROKER_STATE_STANDBY, lifecycle.state)
        self.assertEqual([], activated)

        # Peer returns non-200 / goes away -> standby self-activates after grace elapses.
        peer_alive.clear()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and lifecycle.state != BROKER_STATE_ACTIVE:
            time.sleep(0.01)
        self.assertEqual(BROKER_STATE_ACTIVE, lifecycle.state)
        self.assertEqual(["recovered"], activated)

    def test_standby_watchdog_survives_http_exceptions_and_probe_errors(self) -> None:
        # DNS/network/HTTP/URL errors must fail closed (None) so an active peer
        # hidden behind a transient partition does not cause split-brain activation,
        # while ConnectionRefusedError (0 ready backends after DNS) returns False.
        with mock.patch("http.client.HTTPConnection.request", side_effect=http.client.BadStatusLine("bad")), \
                mock.patch("http.client.HTTPConnection.close"):
            self.assertIsNone(credential_proxy._probe_peer_healthz("http://127.0.0.1:8765/healthz", 0.5))
        with mock.patch("http.client.HTTPConnection.request", side_effect=OSError("gaierror: name resolution failed")), \
                mock.patch("http.client.HTTPConnection.close"):
            self.assertIsNone(credential_proxy._probe_peer_healthz("http://127.0.0.1:8765/healthz", 0.5))
        with mock.patch("http.client.HTTPConnection.request", side_effect=TimeoutError("timed out")), \
                mock.patch("http.client.HTTPConnection.close"):
            self.assertIsNone(credential_proxy._probe_peer_healthz("http://127.0.0.1:8765/healthz", 0.5))
        self.assertIsNone(credential_proxy._probe_peer_healthz("http://[::1:8765/healthz", 0.5))
        with mock.patch("http.client.HTTPConnection.request", side_effect=ConnectionRefusedError("connection refused")), \
                mock.patch("http.client.HTTPConnection.close"):
            self.assertIs(credential_proxy._probe_peer_healthz("http://127.0.0.1:8765/healthz", 0.5), False)

        calls = [0]
        allow_absent = threading.Event()

        def flaky_then_absent() -> bool | None:
            calls[0] += 1
            if not allow_absent.is_set():
                if calls[0] % 2 == 1:
                    raise http.client.IncompleteRead(b"partial")
                return None
            return False

        activated: list[str] = []
        lifecycle = BrokerLifecycle(
            enabled=True,
            control_file=self.token_path,
            peer_active_probe=flaky_then_absent,
            peer_probe_interval_seconds=0.01,
            self_activate_grace_seconds=0.04,
        )
        self.addCleanup(lifecycle.stop_watchdog)
        lifecycle.set_warm(on_activate=lambda: activated.append("survived"))

        # Stay in standby well past the grace window while probes fail/error.
        time.sleep(0.12)
        self.assertEqual(BROKER_STATE_STANDBY, lifecycle.state)
        self.assertEqual([], activated)
        self.assertGreaterEqual(calls[0], 4)

        allow_absent.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and lifecycle.state != BROKER_STATE_ACTIVE:
            time.sleep(0.01)
        self.assertEqual(BROKER_STATE_ACTIVE, lifecycle.state)
        self.assertEqual(["survived"], activated)

    def test_standby_watchdog_resets_grace_on_probe_error_after_absent_probes(self) -> None:
        # False probes for less than grace (0.3s < 0.5s), then None (inconclusive probe error),
        # then False again (< 0.5s): cumulative elapsed exceeds 0.5s, yet None resets
        # no_peer_since so standby stays standby until a full uninterrupted 0.5s of False passes.
        schedule: list[tuple[float, bool | None]] = [
            (0.00, False),
            (0.30, False),
            (0.40, None),   # must reset no_peer_since
            (0.50, False),  # total elapsed >= 0.50s since start, but only 0.00s since reset
            (0.80, False),  # 0.30s since reset (< 0.50s) -> must NOT activate yet
        ]
        idx = [0]
        now_val = [0.0]
        activated: list[str] = []
        done = threading.Event()
        release_final = threading.Event()
        self.addCleanup(release_final.set)

        def scripted_probe() -> bool | None:
            i = idx[0]
            if i < len(schedule):
                t, result = schedule[i]
                now_val[0] = t
                idx[0] += 1
                if idx[0] == len(schedule):
                    done.set()
                return result
            release_final.wait(timeout=2.0)
            now_val[0] = 1.10  # >= 0.50s after 0.50 -> now cleanly activates
            return False

        lifecycle = BrokerLifecycle(
            enabled=True,
            control_file=self.token_path,
            peer_active_probe=scripted_probe,
            peer_probe_interval_seconds=0.001,
            self_activate_grace_seconds=0.50,
            clock=lambda: now_val[0],
        )
        self.addCleanup(lifecycle.stop_watchdog)
        lifecycle.set_warm(on_activate=lambda: activated.append("after-fresh-grace"))

        self.assertTrue(done.wait(timeout=2.0))
        time.sleep(0.02)
        self.assertEqual(BROKER_STATE_STANDBY, lifecycle.state)
        self.assertEqual([], activated)

        release_final.set()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and lifecycle.state != BROKER_STATE_ACTIVE:
            time.sleep(0.01)
        self.assertEqual(BROKER_STATE_ACTIVE, lifecycle.state)
        self.assertEqual(["after-fresh-grace"], activated)

    def test_activate_refuses_while_shutting_down_and_sigterm_stops_watchdog(self) -> None:
        external_stop = threading.Event()
        activated: list[str] = []
        lifecycle = BrokerLifecycle(
            enabled=True,
            control_file=self.token_path,
            peer_active_probe=lambda: False,
            peer_probe_interval_seconds=0.01,
            self_activate_grace_seconds=0.02,
            external_stop=external_stop,
        )
        self.addCleanup(lifecycle.stop_watchdog)
        lifecycle.set_warm(on_activate=lambda: activated.append("should-not-run"))
        external_stop.set()
        status, snap = lifecycle.activate()
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual(BROKER_STATE_STANDBY, snap["state"])
        self.assertEqual([], activated)

        # Even if install_shutdown_handler catches SIGTERM, watchdog stops immediately.
        prev_lifecycle = CredentialProxyHandler.lifecycle
        prev_shutting_down = CredentialProxyHandler.shutting_down.is_set()
        CredentialProxyHandler.shutting_down.clear()
        CredentialProxyHandler.lifecycle = lifecycle
        self.addCleanup(lambda: setattr(CredentialProxyHandler, "lifecycle", prev_lifecycle))
        self.addCleanup(
            lambda: CredentialProxyHandler.shutting_down.set()
            if prev_shutting_down
            else CredentialProxyHandler.shutting_down.clear()
        )
        dummy_server = SimpleNamespace(shutdown=lambda: None)
        with mock.patch("signal.signal") as mock_signal:
            credential_proxy.install_shutdown_handler(dummy_server, [], drain_seconds=0.0, settle_seconds=0.0)
            on_sigterm = mock_signal.call_args.args[1]
            on_sigterm(15, None)
        self.assertTrue(lifecycle._watchdog_stop.is_set())


if __name__ == "__main__":
    unittest.main()
