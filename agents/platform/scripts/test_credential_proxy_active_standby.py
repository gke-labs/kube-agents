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
        ):
            self._saved[attr] = (attr in CredentialProxyHandler.__dict__, CredentialProxyHandler.__dict__.get(attr))
        self.addCleanup(self._restore_handler_attrs)

        CredentialProxyHandler.draining.clear()
        CredentialProxyHandler.handoff_draining.clear()
        CredentialProxyHandler.shutting_down.clear()
        CredentialProxyHandler.slack_relay = None
        CredentialProxyHandler.readings_sweep = None
        CredentialProxyHandler.metrics = ProxyMetrics()
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
        self.assertEqual(10.0, credential_proxy.HANDOFF_DRAIN_SECONDS)
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


if __name__ == "__main__":
    unittest.main()
