"""The adapter pulls, settles and preflights through the credential broker's relay.

A fake broker on a local socket plays the relay: it records every call, answers
pulls with a scripted queue, and can refuse, stall or 404. The adapter module is
loaded the way test_dedup loads it, with Hermes' gateway package stubbed.
"""

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from test_dedup import adapter_mod

LOGGER_NAME = adapter_mod.__name__
PROJECT = "example-project"
SUB_PATH = f"projects/{PROJECT}/subscriptions/stockout-alerts-sub"
ROUTE = "gke_stockout_alerts"
ROUTE_CONFIG = {"subscription": "stockout-alerts-sub", "topic": "stockout-alerts"}
ELAPSED_RE = re.compile(r"\b\d+\.\d+s\b")
PULL_PATH = f"/v1/pubsub/gcp/pubsub.googleapis.com/v1/{SUB_PATH}:pull"
ACK_PATH = f"/v1/pubsub/gcp/pubsub.googleapis.com/v1/{SUB_PATH}:acknowledge"
NACK_PATH = f"/v1/pubsub/gcp/pubsub.googleapis.com/v1/{SUB_PATH}:modifyAckDeadline"
GET_PATH = f"/v1/pubsub/gcp/pubsub.googleapis.com/v1/{SUB_PATH}"


def _message(message_id, payload, ack_id):
    import base64
    return {"ackId": ack_id, "message": {"messageId": message_id, "attributes": {"origin": "test"},
                                         "data": base64.b64encode(json.dumps(payload).encode()).decode()}}


class FakeBroker:
    """A relay that records calls and answers from scripts.

    ``pulls`` is a queue of answers for successive :pull calls: a list of received
    messages, or an int status to refuse with. When it runs dry, pulls answer empty
    after ``idle_delay`` seconds, the way Pub/Sub does when nothing is queued.
    """

    def __init__(self):
        self.calls = []
        self.pulls = []
        self.get_status = 200
        self.get_body = None
        self.get_delay = 0.0
        self.idle_delay = 0.05
        # When set, a scripted pull is answered only once this event is set: a
        # pull parked at the broker past the adapter's stop.
        self.pull_hold = None
        self.lock = threading.Lock()
        broker = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _answer(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    # The stalled-read case: the adapter gave up before the answer.
                    return

            def do_GET(self):  # noqa: N802
                with broker.lock:
                    broker.calls.append(("GET", self.path, dict(self.headers), None))
                time.sleep(broker.get_delay)
                if broker.get_status == 200:
                    self._answer(200, {"name": SUB_PATH})
                else:
                    self._answer(broker.get_status, broker.get_body or {"error": {"code": broker.get_status, "status": "NOT_FOUND"}})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                with broker.lock:
                    broker.calls.append(("POST", self.path, dict(self.headers), body))
                    script = broker.pulls.pop(0) if (self.path.endswith(":pull") and broker.pulls) else None
                if self.path.endswith(":pull"):
                    if script is None:
                        time.sleep(broker.idle_delay)
                        self._answer(200, {})
                    elif isinstance(script, int):
                        self._answer(script, {"status": "blocked", "code": "SECURITY_POLICY_BLOCKED",
                                              "rule": "gcp.pubsub.subscription", "message": "refused"})
                    else:
                        if broker.pull_hold is not None:
                            broker.pull_hold.wait(5)
                        self._answer(200, {"receivedMessages": script})
                else:
                    self._answer(200, {})

            def log_message(self, *_):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                calls = list(self.calls)
            if predicate(calls):
                return calls
            time.sleep(0.02)
        with self.lock:
            return list(self.calls)


class RelayTestCase(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        self.addCleanup(self.broker.close)
        self.token_file = tempfile.NamedTemporaryFile("w", suffix=".token", delete=False)
        self.token_file.write("gateway-token\n")
        self.token_file.close()
        self.addCleanup(os.unlink, self.token_file.name)
        self.env = mock.patch.dict(os.environ, {
            "PUBSUB_RELAY_URL": self.broker.url,
            "CREDENTIAL_PROXY_TOKEN_FILE": self.token_file.name,
            "GCP_PROJECT_ID": PROJECT,
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.adapter = adapter_mod.PubSubAdapter.__new__(adapter_mod.PubSubAdapter)
        self.adapter._subscriptions_config = {ROUTE: dict(ROUTE_CONFIG)}
        self.adapter._relay = None
        self.adapter._pull_threads = []
        self.adapter._stop_pulling = threading.Event()
        self.adapter._running = True
        self.processed = []
        self.fatal = []
        processed, fatal = self.processed, self.fatal

        async def process(route_name, route_config, message):
            processed.append((route_name, message))
            message.ack()

        self.adapter._process_message = process
        self.adapter._set_fatal_error = lambda code, detail, retryable: fatal.append((code, detail, retryable))
        self.adapter._mark_connected = lambda: None
        self.adapter._mark_disconnected = lambda: None
        logging.getLogger(LOGGER_NAME).setLevel(logging.INFO)

    def run_connect(self, connected_for=0.3):
        async def session():
            ok = await self.adapter.connect()
            await asyncio.sleep(connected_for)
            await self.adapter.disconnect()
            return ok
        return asyncio.run(session())


class ConnectTest(RelayTestCase):
    def test_a_message_is_pulled_processed_and_acked_through_the_relay(self):
        self.broker.pulls = [[_message("m-1", {"text": "scale-up error"}, "ack-1")]]
        with self.assertLogs(LOGGER_NAME, logging.INFO) as logs:
            ok = self.run_connect()
        self.assertTrue(ok)
        calls = self.broker.calls
        methods = [(method, path) for method, path, _, _ in calls]
        self.assertIn(("GET", GET_PATH), methods, "the subscription is read first")
        self.assertIn(("POST", PULL_PATH), methods)
        self.assertIn(("POST", ACK_PATH), methods)
        [(route, message)] = self.processed
        self.assertEqual(route, ROUTE)
        self.assertEqual(json.loads(message.data), {"text": "scale-up error"})
        self.assertEqual(message.message_id, "m-1")
        self.assertEqual(message.attributes, {"origin": "test"})
        ack = next(body for method, path, _, body in calls if path == ACK_PATH)
        self.assertEqual(ack, {"ackIds": ["ack-1"]})
        pull = next((headers, body) for method, path, headers, body in calls if path == PULL_PATH)
        self.assertEqual(pull[0]["Authorization"], "Bearer gateway-token")
        self.assertEqual(pull[1], {"maxMessages": adapter_mod.PULL_MAX_MESSAGES})
        text = "\n".join(logs.output)
        self.assertIn(f"Subscribing to '{SUB_PATH}' (route '{ROUTE}')", text)
        self.assertIn(f"Callback entry received message on route '{ROUTE}', msg_id=m-1", text)
        self.assertIn("connected through the credential broker relay", text)

    def test_a_quoted_value_with_a_stray_space_composes_the_same_path_the_operator_renders(self):
        self.adapter._subscriptions_config = {ROUTE: {"subscription": " stockout-alerts-sub ", "topic": "stockout-alerts "}}
        self.run_connect(connected_for=0.1)
        self.assertTrue(any(path == PULL_PATH for _, path, _, _ in self.broker.calls), self.broker.calls)

    def test_a_short_subscription_name_is_qualified_with_the_projects_environment(self):
        self.adapter._subscriptions_config = {ROUTE: {"topic": "stockout-alerts"}}
        self.run_connect(connected_for=0.1)
        self.assertTrue(any(path == PULL_PATH for _, path, _, _ in self.broker.calls), self.broker.calls)

    def test_the_operators_project_wins_over_the_overridable_one(self):
        """PUBSUB_RELAY_PROJECT is the project the broker's set was composed with;
        GCP_PROJECT_ID is a plugin's to override."""
        os.environ["PUBSUB_RELAY_PROJECT"] = "operators-project"
        os.environ["GCP_PROJECT_ID"] = "a-plugins-project"
        self.run_connect(connected_for=0.2)
        pulls = [c for c in self.broker.calls if c[1].endswith(":pull")]
        self.assertTrue(pulls)
        self.assertIn("/v1/projects/operators-project/subscriptions/", pulls[0][1])

    def test_a_route_with_a_short_name_and_no_project_is_skipped_loudly(self):
        for name in adapter_mod.PROJECT_ID_ENVS:
            os.environ.pop(name, None)
        with self.assertLogs(LOGGER_NAME, logging.WARNING) as logs:
            ok = self.run_connect(connected_for=0.1)
        self.assertTrue(ok)
        self.assertEqual(self.broker.calls, [])
        output = "\n".join(logs.output)
        self.assertIn(f"Route '{ROUTE}' skipped: it names no subscription this adapter can pull", output)
        self.assertIn(f"Route '{ROUTE}' names a short subscription or topic and no project id is set", output)
        # The presubmit gate reads the connected line; a connect that started no
        # route must not print it.
        self.assertNotIn("connected through the credential broker relay", output)
        self.assertIn("no route started", output)

    def test_no_relay_url_is_a_fatal_error_not_a_pull_on_a_missing_credential(self):
        # Unset, and the empty value the operator renders when no route qualified.
        for value in (None, ""):
            with self.subTest(value=value):
                self.fatal.clear()
                if value is None:
                    os.environ.pop("PUBSUB_RELAY_URL", None)
                else:
                    os.environ["PUBSUB_RELAY_URL"] = value
                with self.assertLogs(LOGGER_NAME, logging.ERROR) as logs:
                    ok = asyncio.run(self.adapter.connect())
                self.assertFalse(ok)
                self.assertEqual(self.fatal, [("relay_not_configured", "PUBSUB_RELAY_URL not set", False)])
                output = "\n".join(logs.output)
                self.assertIn("PUBSUB_RELAY_URL is empty", output)
                self.assertIn("AgentPlugin's status names a route", output)
                self.assertEqual(self.broker.calls, [])

    def test_a_refused_pull_names_the_rule_and_backs_off(self):
        self.broker.pulls = [403, 403]
        with mock.patch.object(adapter_mod, "PULL_BACKOFF_INITIAL_SECONDS", 0.05), \
                mock.patch.object(adapter_mod, "PULL_BACKOFF_MAX_SECONDS", 0.1), \
                self.assertLogs(LOGGER_NAME, logging.WARNING) as logs:
            self.run_connect(connected_for=0.5)
        text = "\n".join(logs.output)
        self.assertIn(f"pull on route '{ROUTE}' failed (status=403 code=SECURITY_POLICY_BLOCKED rule=gcp.pubsub.subscription", text)
        pulls = [c for c in self.broker.calls if c[1] == PULL_PATH]
        self.assertGreaterEqual(len(pulls), 3, "the loop keeps pulling after a refusal")

    def test_an_empty_pull_is_followed_by_a_pause_not_another_pull(self):
        with mock.patch.object(adapter_mod, "PULL_IDLE_INTERVAL_SECONDS", 0.4):
            self.run_connect(connected_for=1.0)
        pulls = [c for c in self.broker.calls if c[1] == PULL_PATH]
        # ~1 s connected, every answer empty, 0.4 s pause each: three pulls, not dozens.
        self.assertLessEqual(len(pulls), 4, f"{len(pulls)} pulls in a second: the loop spins on an empty backlog")
        self.assertGreaterEqual(len(pulls), 2)

    def test_disconnect_stops_the_pull_thread(self):
        self.run_connect(connected_for=0.2)
        before = len(self.broker.calls)
        time.sleep(0.3)
        self.assertEqual(len(self.broker.calls), before, "a pull arrived after disconnect")
        self.assertEqual(self.adapter._pull_threads, [])

    def test_a_pull_that_returns_after_the_stop_hands_its_messages_back(self):
        """A batch a stopped generation receives is nacked, not processed or held to the deadline."""
        self.broker.pull_hold = threading.Event()
        self.broker.pulls = [[{"ackId": "late-1", "message": {"data": "e30=", "messageId": "m1"}}]]
        threads = []
        with mock.patch.object(adapter_mod, "PULL_THREAD_JOIN_SECONDS", 0.1):
            async def session():
                await self.adapter.connect()
                threads.extend(self.adapter._pull_threads)
                self.broker.wait_for(lambda calls: any(c[1].endswith(":pull") for c in calls))
                await self.adapter.disconnect()
            with self.assertLogs(LOGGER_NAME, logging.WARNING) as logs:
                asyncio.run(session())
                self.broker.pull_hold.set()
                calls = self.broker.wait_for(lambda calls: any(c[1].endswith(":modifyAckDeadline") for c in calls))
                # The thread logs after it has read the nack's answer; wait for
                # the thread, not for the broker's record of the call.
                for thread in threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive(), "the stopped pull thread did not end")
        nacks = [c for c in calls if c[1].endswith(":modifyAckDeadline")]
        self.assertEqual([c[3] for c in nacks], [{"ackIds": ["late-1"], "ackDeadlineSeconds": 0}])
        self.assertFalse([c for c in calls if c[1].endswith(":acknowledge")])
        self.assertEqual(self.processed, [])
        self.assertIn("returned them for redelivery", "\n".join(logs.output))

    def test_a_message_that_cannot_be_decoded_is_left_to_redeliver(self):
        bad = {"ackId": "ack-bad", "message": {"messageId": "m-bad", "data": "%%%not-base64%%%"}}
        self.broker.pulls = [[bad, _message("m-2", {"ok": True}, "ack-2")]]
        with self.assertLogs(LOGGER_NAME, logging.ERROR) as logs:
            self.run_connect()
        self.assertIn("could not be decoded and is left to redeliver", "\n".join(logs.output))
        self.assertEqual([m.message_id for _, m in self.processed], ["m-2"])
        acks = [body for _, path, _, body in self.broker.calls if path == ACK_PATH]
        self.assertEqual(acks, [{"ackIds": ["ack-2"]}])


class SettleTest(RelayTestCase):
    def test_ack_then_nack_settles_once_and_nack_uses_a_zero_deadline(self):
        relay = adapter_mod.PubsubRelay(self.broker.url, self.token_file.name)
        message = adapter_mod.RelayMessage(relay, SUB_PATH, _message("m-1", {}, "ack-1"))
        message.nack()
        message.nack()
        message.ack()
        calls = self.broker.wait_for(lambda c: len(c) >= 1)
        time.sleep(0.2)
        settles = [(path, body) for _, path, _, body in self.broker.calls]
        self.assertEqual(settles, [(NACK_PATH, {"ackIds": ["ack-1"], "ackDeadlineSeconds": 0})])


class PreflightTest(RelayTestCase):
    def setUp(self):
        super().setUp()
        self.adapter._relay = adapter_mod.PubsubRelay(self.broker.url, self.token_file.name)

    def check(self):
        with self.assertLogs(LOGGER_NAME, logging.INFO) as logs:
            result = self.adapter._check_subscription_exists(SUB_PATH)
        return result, logs.output

    def test_the_read_is_announced_before_it_is_issued_and_its_result_timed(self):
        ok, lines = self.check()
        self.assertTrue(ok)
        self.assertIn(f"Checking subscription '{SUB_PATH}' exists (get_subscription, {adapter_mod.PREFLIGHT_RPC_TIMEOUT_SECONDS}s timeout)", lines[0])
        self.assertIn(f"Subscription '{SUB_PATH}' exists", lines[-1])
        self.assertRegex(lines[-1], ELAPSED_RE)
        [(method, path, headers, _)] = self.broker.calls
        self.assertEqual((method, path), ("GET", GET_PATH))
        self.assertEqual(headers["Authorization"], "Bearer gateway-token")

    def test_a_missing_subscription_is_a_warning_that_says_so(self):
        self.broker.get_status = 404
        ok, lines = self.check()
        self.assertFalse(ok)
        self.assertIn(f"Subscription '{SUB_PATH}' is NOT present in GCP", lines[-1])
        self.assertTrue(lines[-1].startswith("WARNING"))

    def test_a_broker_without_the_route_is_named_as_such_not_as_a_missing_subscription(self):
        """The broker's own 404 carries {"status": "not_found"}; Pub/Sub's carries error.status."""
        self.broker.get_status = 404
        self.broker.get_body = {"status": "not_found"}
        ok, lines = self.check()
        self.assertFalse(ok)
        self.assertIn("the credential broker has no /v1/pubsub/gcp/ route", lines[-1])
        self.assertNotIn("NOT present in GCP", lines[-1])

    def test_a_stalled_read_is_bounded_warned_and_survived(self):
        self.broker.get_delay = 0.5
        with mock.patch.object(adapter_mod, "PREFLIGHT_RPC_TIMEOUT_SECONDS", 0.1):
            ok, lines = self.check()
        self.assertFalse(ok)
        self.assertIn("Timed out after", lines[-1])
        self.assertIn("get_subscription", lines[-1])
        self.assertIn("Continuing startup without verifying it", lines[-1])
        self.assertTrue(lines[-1].startswith("WARNING"))

    def test_a_broker_that_is_down_is_named_as_unreachable_not_as_a_timeout(self):
        self.broker.close()
        ok, lines = self.check()
        self.assertFalse(ok)
        self.assertIn("Could not reach the credential broker's relay", lines[-1])
        self.assertNotIn("Timed out", lines[-1])

    def test_a_reconnect_without_a_disconnect_ends_the_previous_generation_first(self):
        self.adapter._relay = None

        async def session():
            await self.adapter.connect()
            first = list(self.adapter._pull_threads)
            await self.adapter.connect(is_reconnect=True)
            second = list(self.adapter._pull_threads)
            await asyncio.sleep(0.3)
            await self.adapter.disconnect()
            return first, second

        first, second = asyncio.run(session())
        self.assertEqual((len(first), len(second)), (1, 1))
        self.assertIsNot(first[0], second[0])
        for thread in first + second:
            thread.join(2)
            self.assertFalse(thread.is_alive(), f"{thread.name} outlived the reconnect")

    def test_a_reconnect_does_not_leave_the_previous_pull_thread_running(self):
        """The old thread sees its own generation's stop event, not the new one's."""
        self.adapter._relay = None

        async def session():
            await self.adapter.connect()
            first = list(self.adapter._pull_threads)
            await self.adapter.disconnect()
            await self.adapter.connect(is_reconnect=True)
            second = list(self.adapter._pull_threads)
            await asyncio.sleep(0.3)
            await self.adapter.disconnect()
            return first, second

        first, second = asyncio.run(session())
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertIsNot(first[0], second[0])
        for thread in first + second:
            thread.join(2)
            self.assertFalse(thread.is_alive(), f"{thread.name} outlived its disconnect")

    def test_a_refusal_by_the_broker_is_a_warning_naming_the_answer(self):
        self.broker.get_status = 403
        ok, lines = self.check()
        self.assertFalse(ok)
        self.assertIn("the relay answered status=403", lines[-1])


if __name__ == "__main__":
    unittest.main()
