"""Unit tests for agentplugins_e2e_test helper functions.

Verifies operator deployment name resolution across Helm and Kustomize installations,
pod selector polling, deployment rollout existence and retry mechanisms, and that
step 17 leaves the gateway Deployment settled before the suite exits.
"""

import os
import subprocess
import unittest
from unittest.mock import MagicMock, call, patch

import tests.e2e.operator.agentplugins_e2e_test as e2e


class AgentPluginsE2EHelpersTest(unittest.TestCase):
    """Unit tests for operator deployment resolution and rollout polling helpers."""

    def test_get_operator_deployment_honors_env_override(self):
        """When OPERATOR_DEPLOYMENT env var is set, return it directly without kubectl."""
        with patch.dict(os.environ, {"OPERATOR_DEPLOYMENT": "custom-operator-manager"}):
            with patch.object(e2e, "get_kubectl_output") as mock_kubectl:
                name = e2e.get_operator_deployment()
                self.assertEqual(name, "custom-operator-manager")
                mock_kubectl.assert_not_called()

    def test_get_operator_deployment_resolves_via_label(self):
        """When OPERATOR_DEPLOYMENT is not set, resolve via operator label selector."""
        with patch.dict(os.environ, {}, clear=True):
            if "OPERATOR_DEPLOYMENT" in os.environ:
                del os.environ["OPERATOR_DEPLOYMENT"]

            with patch.object(e2e, "get_kubectl_output", return_value="kube-agents-controller-manager\n") as mock_out:
                name = e2e.get_operator_deployment()
                self.assertEqual(name, "kube-agents-controller-manager")
                mock_out.assert_called_once_with([
                    "get", "deployment", "-n", e2e.NAMESPACE,
                    "-l", e2e.OPERATOR_LABEL_SELECTOR,
                    "-o", "jsonpath={.items[0].metadata.name}",
                ])

    def test_get_operator_deployment_raises_when_not_found(self):
        """When no deployment matches the label selector, raise AssertionError."""
        with patch.dict(os.environ, {}, clear=True):
            if "OPERATOR_DEPLOYMENT" in os.environ:
                del os.environ["OPERATOR_DEPLOYMENT"]

            with patch.object(e2e, "get_kubectl_output", return_value=""):
                with self.assertRaises(AssertionError) as ctx:
                    e2e.get_operator_deployment()
                self.assertIn("No operator deployment with label", str(ctx.exception))

    def test_poll_operator_pod_matches_operator_label_selector(self):
        """poll_operator_pod polls pod matching OPERATOR_LABEL_SELECTOR."""
        with patch.object(e2e, "poll_pod_with_image", return_value="kube-agents-controller-manager-abc-123") as mock_poll:
            pod = e2e.poll_operator_pod(timeout_sec=5)
            self.assertEqual(pod, "kube-agents-controller-manager-abc-123")
            mock_poll.assert_called_once_with(
                e2e.OPERATOR_LABEL_SELECTOR,
                e2e.OPERATOR_CONTAINER_NAME,
                expected_image="",
                timeout_sec=5,
            )

    def test_poll_operator_pod_timeout_returns_empty(self):
        """When no operator pod matches the selector, return empty string on timeout."""
        with patch.object(e2e, "poll_pod_with_image", return_value=""):
            pod = e2e.poll_operator_pod(timeout_sec=0)
            self.assertEqual(pod, "")

    def test_wait_deployment_rollout_waits_for_existence_and_succeeds(self):
        """wait_deployment_rollout checks existence first, then succeeds on rollout."""
        calls = []

        def mock_run_kubectl(cmd, check=True, capture_output=False):
            calls.append(cmd)
            mock_res = MagicMock(returncode=0)
            return mock_res

        with patch.object(e2e, "run_kubectl", side_effect=mock_run_kubectl):
            e2e.wait_deployment_rollout("kube-agents-controller-manager", timeout="10s")

        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0][:3], ["get", "deployment", "kube-agents-controller-manager"])
        self.assertEqual(calls[1][:3], ["rollout", "status", "deployment/kube-agents-controller-manager"])

    def test_wait_deployment_rollout_retries_transient_error(self):
        """Transient error in rollout status retries and succeeds if resolved before deadline."""
        call_count = {"count": 0}

        def mock_run_kubectl(cmd, check=True, capture_output=False):
            if cmd[0] == "get":
                return MagicMock(returncode=0)
            if cmd[0] == "rollout":
                call_count["count"] += 1
                if call_count["count"] == 1:
                    raise subprocess.CalledProcessError(1, cmd)
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        with patch.object(e2e, "run_kubectl", side_effect=mock_run_kubectl), \
             patch("time.sleep", return_value=None):
            e2e.wait_deployment_rollout("kube-agents-controller-manager", timeout="20s")

        self.assertEqual(call_count["count"], 2)

    def test_wait_deployment_rollout_raises_on_missing_deployment(self):
        """When deployment never appears in API server, wait_deployment_rollout raises TimeoutError."""
        mock_res = MagicMock(returncode=1)
        with patch.object(e2e, "run_kubectl", return_value=mock_res), \
             patch("time.sleep", return_value=None):
            with self.assertRaises(TimeoutError):
                e2e.wait_deployment_rollout("nonexistent-deployment", timeout="5s")

    def test_wait_deployment_generation_change_succeeds_when_min_gen_reached(self):
        """When current generation reaches or exceeds min_gen, return cleanly."""
        with patch.object(e2e, "get_deployment_generation", return_value=3):
            e2e.wait_deployment_generation_change("platform-agent-gateway", min_gen=2, timeout_sec=5)

    def test_wait_deployment_generation_change_retries_and_succeeds(self):
        """Retries through transient errors and lower generations until min_gen is reached."""
        generations = [1, subprocess.CalledProcessError(1, ["cmd"]), 2]

        def mock_get_generation(deployment_name):
            val = generations.pop(0)
            if isinstance(val, Exception):
                raise val
            return val

        with patch.object(e2e, "get_deployment_generation", side_effect=mock_get_generation), \
             patch("time.sleep", return_value=None):
            e2e.wait_deployment_generation_change("platform-agent-gateway", min_gen=2, timeout_sec=10)

        self.assertEqual(len(generations), 0)

    def test_wait_deployment_generation_change_raises_timeout_error(self):
        """When generation never reaches min_gen, poll until timeout and raise TimeoutError."""
        with patch.object(e2e, "get_deployment_generation", return_value=1) as mock_get_gen, \
             patch("time.time", side_effect=[100.0, 100.0, 103.0]), \
             patch("time.sleep", return_value=None):
            with self.assertRaises(TimeoutError) as ctx:
                e2e.wait_deployment_generation_change("platform-agent-gateway", min_gen=2, timeout_sec=2)
            self.assertIn("generation did not reach 2", str(ctx.exception))
            mock_get_gen.assert_called_once_with("platform-agent-gateway")


class ReconcileAndWaitGenerationTest(unittest.TestCase):
    """reconcile_and_wait must be able to use a generation captured before the mutation.

    Reading the generation after applying or deleting the CR races the operator: a
    reconcile that lands first bumps it, and waiting for gen_before + 1 then waits for a
    second bump that never comes.
    """

    def test_uses_caller_supplied_generation_without_rereading(self):
        with patch.object(e2e, "get_deployment_generation") as mock_get_gen, \
             patch.object(e2e, "wait_deployment_generation_change") as mock_wait_gen, \
             patch.object(e2e, "wait_deployment_rollout"):
            e2e.reconcile_and_wait(17)

        mock_get_gen.assert_not_called()
        mock_wait_gen.assert_called_once_with(e2e.GATEWAY_DEPLOYMENT, min_gen=18)

    def test_reads_generation_when_caller_supplies_none(self):
        with patch.object(e2e, "get_deployment_generation", return_value=4) as mock_get_gen, \
             patch.object(e2e, "wait_deployment_generation_change") as mock_wait_gen, \
             patch.object(e2e, "wait_deployment_rollout"):
            e2e.reconcile_and_wait()

        mock_get_gen.assert_called_once_with(e2e.GATEWAY_DEPLOYMENT)
        mock_wait_gen.assert_called_once_with(e2e.GATEWAY_DEPLOYMENT, min_gen=5)


class Step17SettlesRolloutTest(unittest.TestCase):
    """Step 17 is the last step of the suite, so whatever it leaves in flight lands on
    whichever suite runs next. It must withdraw the plugin and wait for the resulting
    rollout before returning, on the success path and after a mid-step failure alike.
    """

    GENERATION = 7

    def _run_kubectl(self, cmd, **kwargs):
        """Records the call, and answers the cleanup's `get` probe and its delete.

        The probe's returncode is what the finally block reads, so the fake tracks whether
        the CR is on the cluster: absent until the step applies it, gone again once the
        step deletes it. `probe_rc` and `delete_rc` override that to stage the failures a
        real cluster produces -- a read that fails for a reason other than absence, and a
        delete that does not land.
        """
        verb = " ".join(cmd[:2])
        self.calls.append(verb)
        if verb == "get agentplugin":
            if self.probe_raises:
                raise subprocess.CalledProcessError(1, cmd)
            rc = self.probe_rc if self.probe_rc is not None else (0 if self.cr_present else 1)
            return MagicMock(returncode=rc, stdout="", stderr="")
        if verb == "delete agentplugin":
            if self.delete_rc == 0:
                self.cr_present = False
            return MagicMock(returncode=self.delete_rc, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    def _read_generation(self, *args, **kwargs):
        """Recorded in call order because when it is read is the property under test."""
        self.calls.append("get_deployment_generation")
        return self.GENERATION

    def _patched(self, healed="LINK", probe_rc=None, probe_raises=False, delete_rc=0):
        """Patches every cluster-touching helper step 17 calls."""
        self.calls = []
        self.cr_present = False
        self.probe_rc = probe_rc
        self.probe_raises = probe_raises
        self.delete_rc = delete_rc
        patches = {
            "log": MagicMock(),
            "profile_plugin_link": MagicMock(return_value="/plugins/link"),
            "get_deployment_generation": MagicMock(side_effect=self._read_generation),
            "apply_kubectl_manifest": MagicMock(
                side_effect=lambda *a, **k: setattr(self, "cr_present", True)
            ),
            "reconcile_and_wait": MagicMock(
                side_effect=lambda *a, **k: self.calls.append("reconcile_and_wait")
            ),
            "agent_exec": MagicMock(
                return_value=MagicMock(stdout="STAGED REACHABLE", stderr="")
            ),
            "restart_agent_pod": MagicMock(),
            "agent_exec_until": MagicMock(return_value=healed),
            "run_kubectl": MagicMock(side_effect=self._run_kubectl),
            "wait_deployment_rollout": MagicMock(
                side_effect=lambda *a, **k: self.calls.append("wait_deployment_rollout")
            ),
        }
        for name, mock in patches.items():
            p = patch.object(e2e, name, mock)
            p.start()
            self.addCleanup(p.stop)
        return patches

    def _last_index(self, name):
        """The last time `name` was called -- the cleanup's calls, not the step body's."""
        self.assertIn(name, self.calls)
        return len(self.calls) - 1 - self.calls[::-1].index(name)

    def test_withdraws_plugin_and_settles_before_returning(self):
        """On success the CR is deleted and the rollout it triggers is waited out."""
        self._patched()

        e2e.step17_verify_link_self_heals_over_a_stale_directory("example/plugin:v1")

        self.assertIn("delete agentplugin", self.calls)
        # The step's own delete must be followed by a reconcile wait, not left in flight.
        delete_idx = self.calls.index("delete agentplugin")
        self.assertIn("reconcile_and_wait", self.calls[delete_idx:])
        # And the step must not return until the Deployment has settled.
        self.assertEqual(self.calls[-1], "wait_deployment_rollout")
        # The cleanup deletes unconditionally -- a probe that fails for a reason other
        # than absence must not be able to skip it -- so on this path there is a second,
        # redundant delete of a CR that is already gone. What must not follow it is a
        # generation wait: nothing is rolling, and waiting for a bump that is not coming
        # would burn DEFAULT_GENERATION_TIMEOUT_SEC and then raise over a passing step.
        cleanup_delete_idx = self._last_index("delete agentplugin")
        self.assertGreater(cleanup_delete_idx, delete_idx)
        self.assertNotIn("reconcile_and_wait", self.calls[cleanup_delete_idx:])

    def test_settles_rollout_even_when_the_step_fails(self):
        """A mid-step failure leaves the CR on the cluster, so the cleanup's own delete is
        what starts the rollout -- and it must be waited out generation-first.

        wait_deployment_rollout on its own cannot do that. It reads the Deployment as it
        stands before the operator reacts to the delete, finds the generation it is already
        observing, and returns against the revision that still has the plugin in it.
        """
        patches = self._patched(healed="STILL-A-DIR")

        with self.assertRaises(AssertionError):
            e2e.step17_verify_link_self_heals_over_a_stale_directory("example/plugin:v1")

        delete_idx = self._last_index("delete agentplugin")
        self.assertIn(
            "reconcile_and_wait",
            self.calls[delete_idx:],
            "the cleanup delete must be followed by a generation-aware wait",
        )
        # Read before the delete, not after: reading it afterwards races the operator, and
        # gen_before + 1 then waits for a bump that has already happened.
        self.assertLess(self._last_index("get_deployment_generation"), delete_idx)
        # Passed through explicitly, for the same reason -- reconcile_and_wait(None) would
        # re-read the generation on the far side of the delete.
        self.assertEqual(
            patches["reconcile_and_wait"].call_args_list[-1],
            call(self.GENERATION),
        )

    def test_a_failed_probe_does_not_skip_the_cleanup_delete(self):
        """A `get` returns non-zero for NotFound and for an API blip alike.

        Reading the second as "already gone" and skipping the delete would leave the
        plugin mounted on the gateway for the next suite -- the leak this block exists to
        prevent -- so the delete does not depend on the probe. Only the wait does.
        """
        for label, kwargs in (
            ("probe returned non-zero", {"probe_rc": 1}),
            ("probe raised", {"probe_raises": True}),
        ):
            with self.subTest(label):
                self._patched(healed="STILL-A-DIR", **kwargs)

                with self.assertRaises(AssertionError):
                    e2e.step17_verify_link_self_heals_over_a_stale_directory("example/plugin:v1")

                self.assertIn("delete agentplugin", self.calls[self._last_index("get agentplugin"):])
                # Nothing was learned before the delete, so the generation-blind wait is
                # the only one available -- but the step still may not return mid-rollout.
                self.assertEqual(self.calls[-1], "wait_deployment_rollout")

    def test_a_delete_that_did_not_land_is_not_waited_on_for_a_generation_bump(self):
        """No delete, no rollout, no bump. Waiting for one burns the generation timeout and
        then reports a TimeoutError over whatever the step actually failed on."""
        patches = self._patched(healed="STILL-A-DIR", delete_rc=1)

        with self.assertRaises(AssertionError):
            e2e.step17_verify_link_self_heals_over_a_stale_directory("example/plugin:v1")

        delete_idx = self._last_index("delete agentplugin")
        self.assertNotIn("reconcile_and_wait", self.calls[delete_idx:])
        self.assertEqual(self.calls[-1], "wait_deployment_rollout")
        # The one reconcile_and_wait on this path is the step body's, after the apply.
        self.assertEqual(patches["reconcile_and_wait"].call_count, 1)


if __name__ == "__main__":
    unittest.main()

