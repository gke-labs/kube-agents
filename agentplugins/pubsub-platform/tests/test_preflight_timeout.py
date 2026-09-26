"""Tests for the Pub/Sub adapter's preflight checks: announced, bounded, non-blocking.

    python3 -m unittest discover -s agentplugins/pubsub-platform/tests -v

connect() verifies that each route's topic and subscription exist before the gateway is
marked connected, and it does so synchronously on the event loop. When one of those
RPCs stalled, nothing had been logged before it and nothing bounded it, so the gateway
sat silent, with its API port unbound, until the startup probe restarted it ten minutes
later. The checks' results are only logged, so a slow answer must not be able to do
that. These tests pin the three properties that stop it:

  * a line naming the resource is logged **before** the RPC is issued, so a stall is
    attributable from the log alone;
  * the RPC is passed the module's timeout for the attempt and a retry that ends at the
    same bound, and a DeadlineExceeded or RetryError from it is a WARNING naming the
    resource and the elapsed time;
  * `_check_resources` still returns the subscription path, so connect() proceeds.

The client library is faked: nothing here touches the network, a cluster, or the
gateway, and `google.cloud.pubsub_v1` is not installed where this suite runs.
"""

import logging
import re
import sys
import types
import unittest

# The loader that imports adapter.py with its gateway dependencies stubbed; importing
# the loaded module rather than the loader keeps one copy of the adapter per process.
from test_dedup import adapter_mod

LOGGER_NAME = adapter_mod.__name__
PROJECT = "example-project"
TOPIC_PATH = f"projects/{PROJECT}/topics/stockout-alerts"
SUB_PATH = f"projects/{PROJECT}/subscriptions/stockout-alerts-sub"
ROUTE_CONFIG = {"topic": "stockout-alerts"}

# Matches the "(1.2s)" / "after 1.2s" elapsed-time rendering the warnings carry.
ELAPSED_RE = re.compile(r"\b\d+\.\d+s\b")


class _NotFound(Exception):
    pass


class _DeadlineExceeded(Exception):
    pass


class _RetryError(Exception):
    pass


class _Aborted(Exception):
    pass


class _ServiceUnavailable(Exception):
    pass


class _Unknown(Exception):
    pass


class _FakeRetry:
    """Records what the adapter asked the retry loop to do; retries nothing itself."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _fake_if_exception_type(*exception_types):
    """The real one returns a predicate closure; a frozenset compares by content instead."""
    return frozenset(exception_types)


class _FakeClient:
    """Stands in for both PublisherClient and SubscriberClient.

    `behaviour` is the exception to raise, or None to return normally. Every call's
    kwargs are recorded so the test can see what timeout the adapter asked for.
    """

    behaviour = None
    calls = []

    def __init__(self):
        type(self).calls.append(("init",))

    def _call(self, rpc, **kwargs):
        type(self).calls.append((rpc, kwargs))
        if type(self).behaviour is not None:
            raise type(self).behaviour
        return object()

    def get_topic(self, **kwargs):
        return self._call("get_topic", **kwargs)

    def get_subscription(self, **kwargs):
        return self._call("get_subscription", **kwargs)


class _FakePublisher(_FakeClient):
    calls = []


class _FakeSubscriber(_FakeClient):
    calls = []


def _install_fake_google():
    """Put a fake `google.cloud.pubsub_v1` and `google.api_core.exceptions` in sys.modules.

    Returns the entries that were there before, so the caller can put them back.
    """
    names = (
        "google", "google.cloud", "google.cloud.pubsub_v1",
        "google.api_core", "google.api_core.exceptions", "google.api_core.retry",
    )
    previous = {name: sys.modules.get(name) for name in names}
    modules = {name: types.ModuleType(name) for name in names}
    modules["google"].cloud = modules["google.cloud"]
    modules["google"].api_core = modules["google.api_core"]
    modules["google.cloud"].pubsub_v1 = modules["google.cloud.pubsub_v1"]
    modules["google.api_core"].exceptions = modules["google.api_core.exceptions"]
    modules["google.api_core"].retry = modules["google.api_core.retry"]
    modules["google.cloud.pubsub_v1"].PublisherClient = _FakePublisher
    modules["google.cloud.pubsub_v1"].SubscriberClient = _FakeSubscriber
    exceptions = modules["google.api_core.exceptions"]
    exceptions.NotFound = _NotFound
    exceptions.DeadlineExceeded = _DeadlineExceeded
    exceptions.RetryError = _RetryError
    exceptions.Aborted = _Aborted
    exceptions.ServiceUnavailable = _ServiceUnavailable
    exceptions.Unknown = _Unknown
    modules["google.api_core.retry"].Retry = _FakeRetry
    modules["google.api_core.retry"].if_exception_type = _fake_if_exception_type
    sys.modules.update(modules)
    return previous


def _restore_modules(previous):
    for name, module in previous.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class PreflightTestCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(_restore_modules, _install_fake_google())
        for client in (_FakePublisher, _FakeSubscriber):
            client.behaviour = None
            client.calls = []
        # __init__ needs a gateway PlatformConfig; _check_resources uses none of the
        # instance state it would set up.
        self.adapter = adapter_mod.PubSubAdapter.__new__(adapter_mod.PubSubAdapter)

    def check_resources(self):
        with self.assertLogs(LOGGER_NAME, level=logging.INFO) as captured:
            result = self.adapter._check_resources("stockout", ROUTE_CONFIG, PROJECT)
        return result, captured.records

    @staticmethod
    def rpc_calls(client):
        return [call for call in client.calls if call[0] != "init"]


class TimeoutTest(PreflightTestCase):
    """The RPC hangs until the client library gives up: the case that held the gateway."""

    def setUp(self):
        super().setUp()
        _FakePublisher.behaviour = _DeadlineExceeded("Deadline Exceeded")

    def test_the_topic_is_announced_before_the_call_is_issued(self):
        """The fake raises, so anything logged after the call would never appear: the
        INFO line being there at all proves it was written before get_topic ran."""
        _, records = self.check_resources()
        announced = [r for r in records if r.levelno == logging.INFO and "Checking topic" in r.getMessage()]
        self.assertEqual(len(announced), 1, [r.getMessage() for r in records])
        self.assertIn(TOPIC_PATH, announced[0].getMessage())
        self.assertIn("get_topic", announced[0].getMessage())
        # ...and it precedes the warning the timeout produces.
        warned = next(i for i, r in enumerate(records) if r.levelno == logging.WARNING)
        self.assertLess(records.index(announced[0]), warned)

    def test_the_timeout_is_a_warning_naming_the_topic_and_the_elapsed_time(self):
        _, records = self.check_resources()
        warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("Timed out", warnings[0])
        self.assertIn(TOPIC_PATH, warnings[0])
        self.assertIn("get_topic", warnings[0])
        self.assertRegex(warnings[0], ELAPSED_RE)
        # No ERROR: a preflight that could not complete is not a failure of the route.
        self.assertFalse([r for r in records if r.levelno >= logging.ERROR])

    def test_check_resources_returns_so_connect_can_proceed(self):
        result, _ = self.check_resources()
        self.assertEqual(result, SUB_PATH)

    def test_the_subscription_is_still_checked_after_the_topic_times_out(self):
        """One stalled check must not skip the other; each is bounded on its own."""
        _, records = self.check_resources()
        messages = [r.getMessage() for r in records]
        self.assertTrue(any("Checking subscription" in m and SUB_PATH in m for m in messages), messages)
        self.assertTrue(any(f"Subscription '{SUB_PATH}' exists" in m for m in messages), messages)

    def test_the_rpcs_are_bounded_for_the_attempt_and_for_the_retry_loop(self):
        """`timeout=` alone bounds one gRPC attempt; the method's default retry would keep
        retrying UNAVAILABLE for up to 600 s. Both have to carry the module's bound."""
        self.check_resources()
        bound = adapter_mod.PREFLIGHT_RPC_TIMEOUT_SECONDS
        transient = frozenset({_Aborted, _ServiceUnavailable, _Unknown})
        for client, rpc, request in (
            (_FakePublisher, "get_topic", {"topic": TOPIC_PATH}),
            (_FakeSubscriber, "get_subscription", {"subscription": SUB_PATH}),
        ):
            calls = self.rpc_calls(client)
            self.assertEqual(len(calls), 1, calls)
            name, kwargs = calls[0]
            self.assertEqual(name, rpc)
            self.assertEqual(kwargs["request"], request)
            self.assertEqual(kwargs["timeout"], bound)
            self.assertIsInstance(kwargs["retry"], _FakeRetry)
            self.assertEqual(kwargs["retry"].kwargs, {"predicate": transient, "timeout": bound})
            self.assertEqual(set(kwargs), {"request", "timeout", "retry"})

    def test_a_retry_loop_that_runs_out_is_a_timeout_too(self):
        """An unreachable endpoint surfaces as retried UNAVAILABLE and ends in RetryError,
        not DeadlineExceeded; it must land in the same warning, with the cause."""
        _FakePublisher.behaviour = _RetryError("Timeout of 30.0s exceeded, last exception: 503 failed to connect")
        result, records = self.check_resources()
        warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
        self.assertEqual(result, SUB_PATH)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("Timed out", warnings[0])
        self.assertIn(TOPIC_PATH, warnings[0])
        self.assertIn("503 failed to connect", warnings[0])
        self.assertRegex(warnings[0], ELAPSED_RE)


class SubscriptionTimeoutTest(PreflightTestCase):
    def setUp(self):
        super().setUp()
        _FakeSubscriber.behaviour = _DeadlineExceeded("Deadline Exceeded")

    def test_a_hanging_get_subscription_is_announced_warned_and_survived(self):
        result, records = self.check_resources()
        messages = [r.getMessage() for r in records]
        self.assertEqual(result, SUB_PATH)
        self.assertTrue(any("Checking subscription" in m and SUB_PATH in m for m in messages), messages)
        warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("get_subscription", warnings[0])
        self.assertIn(SUB_PATH, warnings[0])
        self.assertRegex(warnings[0], ELAPSED_RE)


class UnchangedBranchesTest(PreflightTestCase):
    """The outcomes the checks already had keep their meaning."""

    def test_an_existing_topic_and_subscription_log_exists(self):
        result, records = self.check_resources()
        messages = [r.getMessage() for r in records]
        self.assertEqual(result, SUB_PATH)
        self.assertTrue(any(f"Topic '{TOPIC_PATH}' exists" in m for m in messages), messages)
        self.assertTrue(any(f"Subscription '{SUB_PATH}' exists" in m for m in messages), messages)
        self.assertFalse([r for r in records if r.levelno >= logging.WARNING])

    def test_a_missing_topic_keeps_its_warning(self):
        _FakePublisher.behaviour = _NotFound("404 not found")
        result, records = self.check_resources()
        warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
        self.assertEqual(result, SUB_PATH)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(f"Topic '{TOPIC_PATH}' is NOT present in GCP", warnings[0])
        self.assertNotIn("Timed out", warnings[0])

    def test_any_other_error_keeps_its_warning(self):
        _FakePublisher.behaviour = RuntimeError("permission denied")
        result, records = self.check_resources()
        warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
        self.assertEqual(result, SUB_PATH)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn(f"Could not verify topic '{TOPIC_PATH}'", warnings[0])
        self.assertIn("permission denied", warnings[0])

    def test_a_route_without_a_topic_issues_no_rpc(self):
        with self.assertLogs(LOGGER_NAME, level=logging.INFO):
            result = self.adapter._check_resources("plain", {"subscription": "manual-sub"}, PROJECT)
        self.assertEqual(result, f"projects/{PROJECT}/subscriptions/manual-sub")
        self.assertEqual(_FakePublisher.calls, [])
        self.assertEqual(_FakeSubscriber.calls, [])


if __name__ == "__main__":
    unittest.main()
