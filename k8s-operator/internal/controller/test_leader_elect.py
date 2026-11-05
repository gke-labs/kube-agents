import os
import unittest
import sys
from unittest.mock import MagicMock, patch

# Mock the kubernetes module BEFORE importing leader_elect
mock_kubernetes = MagicMock()
sys.modules['kubernetes'] = mock_kubernetes
sys.modules['kubernetes.client'] = mock_kubernetes.client
sys.modules['kubernetes.client.rest'] = mock_kubernetes.client.rest
sys.modules['kubernetes.config'] = mock_kubernetes.config

# Now we can import leader_elect safely
import leader_elect
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

class TestLeaderElectLogic(unittest.TestCase):
    def setUp(self):
        leader_elect.lease_name = "test-lease"
        leader_elect.namespace = "test-ns"
        leader_elect.pod_name = "pod-1"
        leader_elect.process = None
        leader_elect.is_shutting_down = False
        
    def tearDown(self):
        if leader_elect.process:
            leader_elect.process = None

    def run_one_iteration(self, mock_sleep):
        # mock sleep to stop the loop
        mock_sleep.side_effect = Exception("StopLoop")
        try:
            leader_elect.main()
        except Exception as e:
            if str(e) != "StopLoop":
                raise e

    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.time.sleep")
    def test_acquire_lease_when_no_lease_exists(self, mock_sleep, mock_popen):
        # Set up mocks
        mock_coordination = MagicMock()
        mock_v1 = MagicMock()
        mock_kubernetes.client.CoordinationV1Api.return_value = mock_coordination
        mock_kubernetes.client.CoreV1Api.return_value = mock_v1
        
        # Make read_namespaced_lease raise a 404 ApiException
        mock_api_exception = Exception("Not Found")
        mock_api_exception.status = 404
        mock_kubernetes.client.rest.ApiException = type('ApiException', (Exception,), {})
        
        # Override the mock exception to actually behave like ApiException
        class MockApiException(Exception):
            def __init__(self, status):
                self.status = status
        
        leader_elect.ApiException = MockApiException
        
        mock_coordination.read_namespaced_lease.side_effect = MockApiException(404)
        
        self.run_one_iteration(mock_sleep)
        
        # Verify it tried to create the lease
        mock_coordination.create_namespaced_lease.assert_called_once()
        # Verify it started the process
        mock_popen.assert_called_once()
        # Verify it labelled the pod
        mock_v1.patch_namespaced_pod.assert_called_once()

    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.time.sleep")
    def test_renew_lease_when_leader(self, mock_sleep, mock_popen):
        # Mock that we are already the leader
        mock_process = MagicMock()
        mock_process.poll.return_value = None
        leader_elect.process = mock_process
        
        mock_coordination = MagicMock()
        mock_kubernetes.client.CoordinationV1Api.return_value = mock_coordination
        
        mock_lease = MagicMock()
        mock_lease.spec.holder_identity = "pod-1"
        mock_coordination.read_namespaced_lease.return_value = mock_lease
        
        self.run_one_iteration(mock_sleep)
        
        # Verify it updated the lease
        mock_coordination.replace_namespaced_lease.assert_called_once()
        # Ensure it didn't try to start a new process
        mock_popen.assert_not_called()

    @patch("leader_elect.time.sleep")
    def test_do_nothing_when_someone_else_is_leader(self, mock_sleep):
        mock_coordination = MagicMock()
        mock_kubernetes.client.CoordinationV1Api.return_value = mock_coordination
        
        mock_lease = MagicMock()
        mock_lease.spec.holder_identity = "pod-2"
        # Not expired
        mock_lease.spec.renew_time = datetime.now(timezone.utc)
        mock_lease.spec.lease_duration_seconds = 15
        
        mock_coordination.read_namespaced_lease.return_value = mock_lease
        
        self.run_one_iteration(mock_sleep)
        
        # Verify it didn't try to acquire or replace the lease
        mock_coordination.replace_namespaced_lease.assert_not_called()

    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.time.sleep")
    def test_take_over_expired_lease(self, mock_sleep, mock_popen):
        mock_coordination = MagicMock()
        mock_v1 = MagicMock()
        mock_kubernetes.client.CoordinationV1Api.return_value = mock_coordination
        mock_kubernetes.client.CoreV1Api.return_value = mock_v1
        
        mock_lease = MagicMock()
        mock_lease.spec.holder_identity = "pod-2"
        # Expired: renewed 20 seconds ago, duration is 15
        mock_lease.spec.renew_time = datetime.now(timezone.utc) - timedelta(seconds=20)
        mock_lease.spec.lease_duration_seconds = 15
        
        mock_coordination.read_namespaced_lease.return_value = mock_lease
        
        self.run_one_iteration(mock_sleep)
        
        # Verify it tried to acquire the lease
        mock_coordination.replace_namespaced_lease.assert_called_once()
        args, kwargs = mock_coordination.replace_namespaced_lease.call_args
        self.assertEqual(kwargs['body'].spec.holder_identity, "pod-1")
        
        # Verify it started the process
        mock_popen.assert_called_once()


class TestGatewayProfile(unittest.TestCase):
    """The gateway argv this wrapper supervises, at spec.harness.experimental.platformFrontDoor.

    At one replica the operator puts `hermes --profile platform gateway run` straight into
    the container args and this file never runs. Above one it runs INSTEAD of those args, so
    the profile has to arrive as an environment variable — and if it does not arrive at all,
    an HA install silently keeps serving chat from the Chat Agent while the CR says otherwise.

    `--profile` is a global flag that hermes_cli/main.py pre-parses out of argv before any
    import, so its position ahead of `gateway` is part of the contract, not formatting.
    HERMES_COMMAND is built at import time, hence the reload.
    """

    def _command(self, value):
        import importlib
        with patch.dict(os.environ, {} if value is None else {"HERMES_GATEWAY_PROFILE": value},
                        clear=False):
            if value is None:
                os.environ.pop("HERMES_GATEWAY_PROFILE", None)
            return importlib.reload(leader_elect).HERMES_COMMAND

    def tearDown(self):
        # Leave the module as the other tests in this file expect to find it.
        self._command(None)

    def test_no_profile_runs_the_default_gateway(self):
        self.assertEqual(self._command(None), ["hermes", "gateway", "run"])

    def test_an_empty_profile_is_not_a_profile(self):
        """An unset variable and one set to "" must mean the same thing.

        Kubernetes renders an absent value as the empty string rather than omitting the
        variable, so `hermes --profile "" gateway run` is what a half-wired manifest would
        produce — and Hermes reads that as a profile named "", homing the gateway at a
        directory nothing scaffolded.
        """
        self.assertEqual(self._command(""), ["hermes", "gateway", "run"])
        self.assertEqual(self._command("  "), ["hermes", "gateway", "run"])

    def test_the_profile_precedes_the_subcommand(self):
        self.assertEqual(
            self._command("platform"),
            ["hermes", "--profile", "platform", "gateway", "run"],
            "`--profile` is global: after `gateway` it is not the same command",
        )

    @patch("leader_elect.time.sleep")
    def test_the_supervised_process_gets_the_profile(self, mock_sleep):
        """The argv is not merely built, it is what Popen is handed."""
        import importlib
        with patch.dict(os.environ, {"HERMES_GATEWAY_PROFILE": "platform"}):
            module = importlib.reload(leader_elect)
        module.lease_name, module.namespace, module.pod_name = "l", "ns", "pod-1"
        module.process, module.is_shutting_down = None, False

        mock_coordination = MagicMock()
        mock_kubernetes.client.CoordinationV1Api.return_value = mock_coordination
        mock_kubernetes.client.CoreV1Api.return_value = MagicMock()
        lease = MagicMock()
        lease.spec.holder_identity = "pod-1"
        mock_coordination.read_namespaced_lease.return_value = lease

        with patch.object(module.subprocess, "Popen") as popen:
            mock_sleep.side_effect = Exception("StopLoop")
            with self.assertRaises(Exception):
                module.main()

        popen.assert_called_once_with(["hermes", "--profile", "platform", "gateway", "run"])


class _Clock:
    """time.monotonic for the drain: advances only when Hermes is waited on."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class _Hermes:
    """A Hermes child that exits `exit_after` seconds after SIGTERM, or only on
    SIGKILL when `exit_after` is None. Waiting on it moves the clock."""

    def __init__(self, clock, events, exit_after):
        self.clock, self.events, self.exit_after = clock, events, exit_after
        self.exit_at = None
        self.returncode = None
        self.terminate = MagicMock(side_effect=self._terminate)
        self.kill = MagicMock(side_effect=self._kill)

    def _terminate(self):
        self.events.append(("sigterm", self.clock.now))
        if self.exit_at is None and self.exit_after is not None:
            self.exit_at = self.clock.now + self.exit_after

    def _kill(self):
        if self.returncode is None:
            self.returncode = -9
            self.events.append(("exit", self.clock.now))

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        if timeout is None:
            raise AssertionError("unbounded wait on a running Hermes")
        if self.exit_at is not None and self.clock.now + timeout >= self.exit_at:
            self.clock.now = max(self.clock.now, self.exit_at)
            self.returncode = 0
            self.events.append(("exit", self.clock.now))
            return 0
        self.clock.now += timeout
        raise leader_elect.subprocess.TimeoutExpired("hermes", timeout)


class _Leases:
    """The coordination API, holding one Lease. `taken_at` hands it to pod-2 at
    that time; `unreachable_from` makes every call from that time on time out,
    spending its whole _request_timeout first; `fail_at` makes calls whose time
    is in that set raise after spending `call_seconds`; `call_seconds` is what
    every other call spends."""

    def __init__(self, clock, events, holder="pod-1", taken_at=None, standby=False, unreachable_from=None, fail_at=(), call_seconds=0):
        self.clock, self.events, self.holder = clock, events, holder
        self.taken_at, self.standby, self.unreachable_from = taken_at, standby, unreachable_from
        self.fail_at = set(fail_at)
        self.call_seconds = call_seconds
        self.request_timeouts = []
        self.renew_time_at = 0.0

    def _check(self, kwargs):
        timeout = kwargs.get("_request_timeout")
        self.request_timeouts.append(timeout)
        if self.unreachable_from is not None and self.clock.now >= self.unreachable_from:
            self.clock.now += timeout if timeout is not None else 3600
            raise RuntimeError("apiserver unreachable")
        if self.clock.now in self.fail_at:
            self.fail_at.remove(self.clock.now)
            self.clock.now += self.call_seconds
            raise leader_elect.ApiException(503) if isinstance(leader_elect.ApiException, type) and issubclass(leader_elect.ApiException, BaseException) else RuntimeError("apiserver 503")
        self.clock.now += self.call_seconds
        if self.taken_at is not None and self.clock.now >= self.taken_at:
            self.holder = "pod-2"

    def read_namespaced_lease(self, name, namespace, **kwargs):
        self._check(kwargs)
        spec = SimpleNamespace(holder_identity=self.holder, renew_time=datetime.now(timezone.utc),
                               lease_duration_seconds=leader_elect.LEASE_DURATION_SECONDS,
                               acquire_time=None, lease_transitions=0)
        return SimpleNamespace(spec=spec)

    def replace_namespaced_lease(self, name, namespace, body, **kwargs):
        self._check(kwargs)
        self.holder = body.spec.holder_identity
        self.renew_time_at = self.clock.now - self.call_seconds
        if self.standby and self.holder == "pod-1":
            self.taken_at = self.renew_time_at + leader_elect.LEASE_DURATION_SECONDS
        self.events.append(("renew" if self.holder == "pod-1" else "release", self.clock.now))


class _Pods:
    def __init__(self, clock, events, call_seconds=0):
        self.clock, self.events, self.call_seconds = clock, events, call_seconds
        self.request_timeouts = []

    def patch_namespaced_pod(self, name, namespace, body, **kwargs):
        self.request_timeouts.append(kwargs.get("_request_timeout"))
        self.clock.now += self.call_seconds
        self.events.append(("label", body["metadata"]["labels"]["kubeagents.io/is-leader"], self.clock.now))


class TestDrainHoldsLease(unittest.TestCase):
    """While Hermes drains after SIGTERM, leader_elect.py keeps renewing the lease,
    and only after Hermes has exited does it clear the leader label and release the
    lease. A standby takes an unrenewed lease after LEASE_DURATION_SECONDS and starts
    its own Hermes at once, so a drain longer than that without renewal would run two
    gateways against one home. Hermes is SIGKILLed rather than waited on when the
    budget runs out or the lease can no longer be confirmed as this pod's.
    """

    # SIGTERM lands just before the main loop's next renewal: the last one was a
    # full poll interval plus jitter and probe timeout ago.
    LAST_RENEWAL = -(
        leader_elect.LEASE_POLL_INTERVAL_SECONDS
        + leader_elect.LEASE_POLL_JITTER_SECONDS
        + leader_elect.HERMES_PROBE_TIMEOUT_SECONDS
    )
    # The gateway pod's terminationGracePeriodSeconds (gatewayTerminationGracePeriodSeconds).
    POD_GRACE_SECONDS = 60
    # Clock skew between nodes the margin before a standby's takeover must absorb.
    CLOCK_SKEW_SECONDS = leader_elect.LEASE_CLOCK_SKEW_SECONDS

    def setUp(self):
        leader_elect.lease_name = "test-lease"
        leader_elect.namespace = "test-ns"
        leader_elect.pod_name = "pod-1"
        leader_elect.is_shutting_down = False
        self.clock = _Clock()
        self.events = []
        leader_elect.lease_renewed_at = self.LAST_RENEWAL
        self.pods = _Pods(self.clock, self.events)
        mock_kubernetes.client.CoreV1Api.return_value = self.pods
        self.exited_at = []
        exit_mock = MagicMock(side_effect=lambda code: self.exited_at.append(self.clock.now))
        for target, value in (("leader_elect.time.monotonic", self.clock.monotonic),
                              ("leader_elect.sys.exit", exit_mock)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        leader_elect.process = None
        leader_elect.is_shutting_down = False
        leader_elect.lease_renewed_at = None

    def _shut_down(self, exit_after, pod_call_seconds=0, **lease_kwargs):
        self.pods.call_seconds = pod_call_seconds
        self.leases = _Leases(self.clock, self.events, **lease_kwargs)
        mock_kubernetes.client.CoordinationV1Api.return_value = self.leases
        self.hermes = _Hermes(self.clock, self.events, exit_after)
        leader_elect.process = self.hermes
        leader_elect.release_lease_and_exit(15, None)
        leader_elect.sys.exit.assert_called_once_with(0)

    def _time_of(self, kind):
        return next(event[-1] for event in self.events if event[0] == kind)

    def test_lease_is_renewed_through_a_drain_longer_than_the_lease(self):
        drain = 40
        self.assertGreater(drain, leader_elect.LEASE_DURATION_SECONDS)
        self._shut_down(exit_after=drain)

        self.hermes.kill.assert_not_called()
        self.assertEqual(self._time_of("exit"), drain)
        renewals = [self.LAST_RENEWAL] + [event[-1] for event in self.events if event[0] == "renew"]
        self.assertGreaterEqual(len(renewals) - 1, drain // leader_elect.LEASE_POLL_INTERVAL_SECONDS)
        # No moment from the last loop renewal to Hermes' exit is further than a
        # lease duration from a renewal before it, so no standby can take over.
        gaps = [b - a for a, b in zip(renewals, renewals[1:] + [drain])]
        self.assertLess(max(gaps), leader_elect.LEASE_DURATION_SECONDS, gaps)
        # Every call is bounded, on a client with retries off, so a hung API
        # cannot stall the drain or the release.
        self.assertTrue(all(timeout == leader_elect.LEASE_API_TIMEOUT_SECONDS
                            for timeout in self.leases.request_timeouts + self.pods.request_timeouts),
                        (self.leases.request_timeouts, self.pods.request_timeouts))
        self.assertEqual(mock_kubernetes.client.Configuration.get_default_copy.return_value.retries, 0)

    def test_lease_is_released_and_label_cleared_after_hermes_exits(self):
        self._shut_down(exit_after=30)

        kinds = [event[0] for event in self.events]
        exit_index = kinds.index("exit")
        self.assertEqual(kinds[exit_index + 1:], ["label", "release"])
        self.assertEqual(self.events[exit_index + 1][1], None)
        self.assertIsNone(self.leases.holder)

    def test_nothing_is_released_while_hermes_is_still_running(self):
        # A Hermes that never finishes draining: the lease stays held for the
        # whole budget, Hermes is killed, and only then is anything handed back.
        self._shut_down(exit_after=None)

        self.hermes.kill.assert_called_once()
        exited = self._time_of("exit")
        self.assertEqual(exited, leader_elect.GATEWAY_SHUTDOWN_TIMEOUT_SECONDS)
        before_exit = [event for event in self.events if event[-1] < exited]
        self.assertTrue(before_exit)
        self.assertEqual({event[0] for event in before_exit}, {"sigterm", "renew"})
        kinds = [event[0] for event in self.events]
        self.assertEqual(kinds[kinds.index("exit") + 1:], ["label", "release"])

    def test_hermes_is_killed_when_another_pod_holds_the_lease(self):
        taken_at = 12
        self._shut_down(exit_after=None, taken_at=taken_at)

        self.hermes.kill.assert_called_once()
        self.assertLessEqual(self._time_of("exit"), taken_at + leader_elect.LEASE_POLL_INTERVAL_SECONDS)
        # The other pod's lease is left alone; this pod's label still goes.
        self.assertEqual(self.leases.holder, "pod-2")
        self.assertNotIn("release", [event[0] for event in self.events])
        self.assertIn(("label", None), [event[:2] for event in self.events])

    def test_hermes_is_killed_when_renewal_goes_unconfirmed_for_a_lease_duration(self):
        self._shut_down(exit_after=None, unreachable_from=3)

        self.hermes.kill.assert_called_once()
        last_renewal = max(event[-1] for event in self.events if event[0] == "renew")
        # The last renewal attempt starts inside LEASE_RENEW_DEADLINE_SECONDS and
        # takes at most one LEASE_API_TIMEOUT_SECONDS read before timing out and
        # killing Hermes.
        self.assertLessEqual(self._time_of("exit"),
                             last_renewal + leader_elect.LEASE_RENEW_DEADLINE_SECONDS + leader_elect.LEASE_API_TIMEOUT_SECONDS)
        self.assertIn(("label", None), [event[:2] for event in self.events])

    def test_a_hung_api_cannot_hold_hermes_past_the_lease(self):
        # The API stops answering mid-drain and every call then burns its whole
        # timeout. Hermes must still be dead before a standby may take the lease,
        # with room left for clock skew between the two nodes.
        self._shut_down(exit_after=None, unreachable_from=3)

        last_renewal = max(event[-1] for event in self.events if event[0] == "renew")
        self.assertLessEqual(self._time_of("exit") + self.CLOCK_SKEW_SECONDS,
                             last_renewal + leader_elect.LEASE_DURATION_SECONDS)

    def test_shutdown_finishes_inside_the_pod_grace_period(self):
        # Hermes never drains and every call takes its full timeout: the kill,
        # label clear and lease release still finish before the kubelet's SIGKILL.
        self._shut_down(exit_after=None, call_seconds=leader_elect.LEASE_API_TIMEOUT_SECONDS,
                        pod_call_seconds=leader_elect.LEASE_API_TIMEOUT_SECONDS)

        self.hermes.kill.assert_called_once()
        self.assertEqual([event[0] for event in self.events][-2:], ["label", "release"])
        self.assertLess(self.exited_at[-1], self.POD_GRACE_SECONDS)

    def test_a_single_unconfirmed_renewal_mid_drain_retries_and_recovers(self):
        # One failed renewal at t=5 retries after LEASE_RENEWAL_RETRY_SECONDS (2s)
        # instead of sleeping the full 5s slice into the 10s renew deadline.
        self._shut_down(exit_after=20, fail_at={5})

        self.hermes.kill.assert_not_called()
        self.assertEqual(self._time_of("exit"), 20)
        renewals = [event[-1] for event in self.events if event[0] == "renew"]
        self.assertIn(5 + leader_elect.LEASE_RENEWAL_RETRY_SECONDS, renewals)

    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.time.sleep")
    def test_lost_leadership_kills_hermes_without_waiting_out_the_drain(self, mock_sleep, mock_popen):
        """Another pod already leads, so a drain here would be two gateways at
        once: the loop stops Hermes through the same path, which sees the other
        holder on its first renewal and kills it, then the label is cleared."""
        self.leases = _Leases(self.clock, self.events, holder="pod-2")
        mock_kubernetes.client.CoordinationV1Api.return_value = self.leases
        self.hermes = _Hermes(self.clock, self.events, exit_after=None)
        leader_elect.process = self.hermes
        leader_elect.label_state = True
        mock_sleep.side_effect = Exception("StopLoop")

        with self.assertRaisesRegex(Exception, "StopLoop"):
            leader_elect.main()

        self.hermes.terminate.assert_called_once()
        self.hermes.kill.assert_called_once()
        self.assertEqual(self._time_of("exit"), 0)
        self.assertIsNone(leader_elect.process)
        mock_popen.assert_not_called()
        self.assertEqual(self.leases.holder, "pod-2")
        self.assertNotIn("renew", [event[0] for event in self.events])
        self.assertEqual(self.events[-1][:2], ("label", None))

    @patch("leader_elect.hermes_answers", return_value=True)
    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.time.sleep")
    def test_transient_lease_error_while_leading_keeps_hermes_running_inside_renew_deadline(
        self, mock_sleep, mock_popen, _mock_answers
    ):
        """A transient 503 reading or replacing the lease while leading has not
        observed another holder, so Hermes stays running inside the renew
        deadline and is stopped only once that deadline passes without a renewal."""
        class MockApiException(Exception):
            def __init__(self, status=503):
                self.status = status

        self.addCleanup(setattr, leader_elect, "ApiException", leader_elect.ApiException)
        leader_elect.ApiException = MockApiException

        self.leases = _Leases(self.clock, self.events, holder="pod-1", fail_at={5, 10})
        mock_kubernetes.client.CoordinationV1Api.return_value = self.leases
        self.hermes = _Hermes(self.clock, self.events, exit_after=None)
        leader_elect.process = self.hermes
        leader_elect.lease_renewed_at = 0.0
        self.clock.now = 5.0
        mock_sleep.side_effect = Exception("StopLoop")

        # At t=5 (inside the 10s renew deadline), a 503 does not stop Hermes.
        with self.assertRaisesRegex(Exception, "StopLoop"):
            leader_elect.main()

        self.hermes.terminate.assert_not_called()
        self.hermes.kill.assert_not_called()
        self.assertIs(leader_elect.process, self.hermes)
        mock_popen.assert_not_called()

        # At t=10 (at the renew deadline), a persistent 503 enters the stop path.
        self.clock.now = 10.0
        self.leases.fail_at = {10.0}
        with self.assertRaisesRegex(Exception, "StopLoop"):
            leader_elect.main()

        self.hermes.terminate.assert_called_once()
        self.hermes.kill.assert_called_once()
        self.assertIsNone(leader_elect.process)

    @patch("leader_elect.hermes_answers", return_value=True)
    @patch("leader_elect.subprocess.Popen")
    @patch("leader_elect.time.sleep")
    def test_drain_renews_on_entry_after_slow_probe_and_sleep(self, mock_sleep, mock_popen, _mock_answers):
        """A 3s probe plus a 7s sleep after the main loop's renewal already reaches
        the 10s renew deadline while the 15s lease is still valid; main() stamps
        lease_renewed_at immediately before the replace (at the same instant as
        renew_time), and the drain renews on entry when the renewal plus clock skew
        still fits inside LEASE_DURATION_SECONDS, killing otherwise."""
        self.leases = _Leases(self.clock, self.events, holder="pod-1", call_seconds=1.5)
        mock_kubernetes.client.CoordinationV1Api.return_value = self.leases
        self.hermes = _Hermes(self.clock, self.events, exit_after=30)
        leader_elect.process = self.hermes
        mock_sleep.side_effect = Exception("StopLoop")

        with self.assertRaisesRegex(Exception, "StopLoop"):
            leader_elect.main()

        # main() stamped lease_renewed_at right before the replace (after the 1.5s
        # read, at t=1.5), matching the moment renew_time was taken.
        self.assertEqual(leader_elect.lease_renewed_at, 1.5)
        self.assertEqual(self.leases.renew_time_at, 1.5)

        # Advance 10.0s past that renewal (3s probe + 7s sleep -> t=11.5) and
        # deliver SIGTERM against a healthy apiserver and a standby waiting at
        # renew_time + LEASE_DURATION_SECONDS (t=16.5): the 3.0s entry renewal
        # finishes at t=14.5, pushing the standby's takeover out and keeping
        # Hermes alive through its 30s drain.
        self.clock.now = leader_elect.lease_renewed_at + 10.0
        self.events.clear()
        self._shut_down(exit_after=30, call_seconds=1.5, standby=True,
                        taken_at=leader_elect.lease_renewed_at + leader_elect.LEASE_DURATION_SECONDS)

        self.hermes.kill.assert_not_called()
        self.assertEqual(self._time_of("renew"), 14.5)
        self.assertEqual(self._time_of("exit"), 41.5)

        # If SIGTERM lands past lease_renewed_at + LEASE_DURATION_SECONDS -
        # LEASE_RENEWAL_MAX_SECONDS - LEASE_CLOCK_SKEW_SECONDS (e.g. +10.5s),
        # an entry renewal could reach a standby's takeover at renew_time + 15s,
        # so Hermes is killed on iteration 1 before the standby takes the lease.
        leader_elect.is_shutting_down = False
        leader_elect.sys.exit.reset_mock()
        self.clock.now = 12.0
        leader_elect.lease_renewed_at = 1.5
        self.events.clear()
        self._shut_down(exit_after=30, call_seconds=1.5,
                        taken_at=leader_elect.lease_renewed_at + leader_elect.LEASE_DURATION_SECONDS)

        self.hermes.kill.assert_called_once()
        self.assertNotIn("renew", [event[0] for event in self.events])
        self.assertEqual(self._time_of("exit"), 12.0)

    def test_budgets_nest(self):
        # Hermes' stop path (30s cron drain + 10s cleanup) fits inside the
        # supervisor's budget; the budget plus the label clear and lease release
        # fits inside the pod's grace; a renewal in flight at the renew deadline
        # still ends before a standby may take the lease; the drain's poll slice
        # sits under the renew deadline; and the main loop's probe plus sleep
        # plus an entry renewal and clock skew fit inside the 15s lease.
        self.assertGreaterEqual(leader_elect.GATEWAY_SHUTDOWN_TIMEOUT_SECONDS, 40)
        self.assertLess(leader_elect.GATEWAY_SHUTDOWN_TIMEOUT_SECONDS + 3 * leader_elect.LEASE_API_TIMEOUT_SECONDS,
                        self.POD_GRACE_SECONDS)
        self.assertLessEqual(leader_elect.LEASE_RENEW_DEADLINE_SECONDS + leader_elect.LEASE_RENEWAL_MAX_SECONDS
                             + self.CLOCK_SKEW_SECONDS, leader_elect.LEASE_DURATION_SECONDS)
        self.assertLess(leader_elect.LEASE_POLL_INTERVAL_SECONDS + leader_elect.LEASE_POLL_JITTER_SECONDS,
                        leader_elect.LEASE_RENEW_DEADLINE_SECONDS)
        self.assertLessEqual(leader_elect.LEASE_POLL_INTERVAL_SECONDS + leader_elect.LEASE_POLL_JITTER_SECONDS
                             + leader_elect.HERMES_PROBE_TIMEOUT_SECONDS + leader_elect.LEASE_RENEWAL_MAX_SECONDS
                             + self.CLOCK_SKEW_SECONDS,
                             leader_elect.LEASE_DURATION_SECONDS)


if __name__ == '__main__':
    unittest.main()
