"""Unit tests for no_live_kubectl, the guard that keeps a test module off a cluster.

Run:
  python3 -m unittest discover -s agents/platform/scripts -p 'test_no_live_kubectl.py' -v

Each test installs a guard of its own, so a call the stand-in records here is
the test's subject and not a leak.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import no_live_kubectl  # noqa: E402

STATE_READ = ["kubectl", "get", "configmap", "platform-agent-gitops-state", "-n", "kubeagents-system", "-o", "json"]


class GuardTestCase(unittest.TestCase):
    def install(self):
        guard = no_live_kubectl.install()
        self.addCleanup(self._discard, guard)
        return guard

    @staticmethod
    def _discard(guard):
        # A test that has not uninstalled leaves the environment as it found it.
        while no_live_kubectl._ACTIVE is guard:
            try:
                guard.uninstall()
            except AssertionError:
                pass


class TestBarriers(GuardTestCase):
    def test_kubeconfig_is_an_empty_file_of_the_guards(self):
        guard = self.install()
        self.assertEqual(os.environ["KUBECONFIG"], str(guard.kubeconfig))
        self.assertEqual(guard.kubeconfig.read_text(), "")

    def test_the_stand_in_answers_a_kubectl_process_and_records_it(self):
        guard = self.install()
        done = subprocess.run(["kubectl", "version", "--client"], capture_output=True, text=True)
        self.assertEqual(done.returncode, no_live_kubectl.STAND_IN_EXIT_CODE)
        self.assertIn(no_live_kubectl.STAND_IN_MESSAGE, done.stderr)
        self.assertEqual(guard.calls(), ["version --client"])

    def test_a_kubectl_run_through_a_shell_reaches_the_stand_in_too(self):
        guard = self.install()
        done = subprocess.run(["sh", "-c", 'kubectl get pods -A; echo "kubeconfig=$KUBECONFIG"'], capture_output=True, text=True)
        self.assertEqual(guard.calls(), ["get pods -A"])
        self.assertIn(f"kubeconfig={guard.kubeconfig}", done.stdout)

    def test_the_state_read_fails_in_the_process_and_starts_none(self):
        guard = self.install()
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            subprocess.run(STATE_READ, capture_output=True, text=True, check=True)
        self.assertEqual(raised.exception.stderr, no_live_kubectl.STATE_READ_ERROR)
        unchecked = subprocess.run(STATE_READ, capture_output=True, text=True)
        self.assertEqual(unchecked.returncode, no_live_kubectl.STATE_READ_EXIT_CODE)
        self.assertEqual(guard.calls(), [])

    def test_a_read_of_another_configmap_goes_to_the_stand_in(self):
        guard = self.install()
        done = subprocess.run(["kubectl", "get", "configmap", "litellm-config"], capture_output=True, text=True)
        self.assertEqual(done.returncode, no_live_kubectl.STAND_IN_EXIT_CODE)
        self.assertEqual(guard.calls(), ["get configmap litellm-config"])

    def test_the_state_configmap_is_the_one_the_environment_names(self):
        guard = self.install()
        renamed = STATE_READ[:3] + ["other-agent-gitops-state"] + STATE_READ[4:]
        with patch.dict(os.environ, {no_live_kubectl.STATE_CONFIGMAP_ENV: "other-agent-gitops-state"}):
            done = subprocess.run(renamed, capture_output=True, text=True)
        self.assertEqual(done.returncode, no_live_kubectl.STATE_READ_EXIT_CODE)
        self.assertEqual(guard.calls(), [])

    def test_a_tests_own_patch_still_answers_the_state_read(self):
        guard = self.install()
        answer = CompletedProcess(STATE_READ, 0, stdout='{"data": {}}', stderr="")
        with patch("subprocess.run", return_value=answer) as run:
            self.assertIs(subprocess.run(STATE_READ, check=True), answer)
        run.assert_called_once()
        self.assertEqual(guard.calls(), [])

    def test_other_commands_run_as_before(self):
        self.install()
        done = subprocess.run([sys.executable, "-c", "print('ran')"], capture_output=True, text=True, check=True)
        self.assertEqual(done.stdout.strip(), "ran")

    def test_the_gitops_state_read_is_unreadable_without_a_process(self):
        guard = self.install()
        no_live_kubectl.assert_in_force(self, guard)


class TestUninstall(GuardTestCase):
    def test_it_restores_the_environment_and_subprocess_run(self):
        before = {name: os.environ.get(name) for name in no_live_kubectl.GUARDED_ENV}
        real_run = subprocess.run
        guard = no_live_kubectl.install()
        stand_in = guard.stand_in
        guard.uninstall()
        self.assertEqual({name: os.environ.get(name) for name in no_live_kubectl.GUARDED_ENV}, before)
        self.assertIs(subprocess.run, real_run)
        self.assertFalse(stand_in.exists())

    def test_it_restores_an_unset_kubeconfig_as_unset(self):
        with patch.dict(os.environ):
            os.environ.pop("KUBECONFIG", None)
            no_live_kubectl.install().uninstall()
            self.assertNotIn("KUBECONFIG", os.environ)

    def test_it_fails_the_module_when_the_stand_in_was_called(self):
        guard = no_live_kubectl.install()
        subprocess.run(["kubectl", "get", "secret", "-n", "kubeagents-system"], capture_output=True)
        with self.assertRaises(AssertionError) as raised:
            guard.uninstall()
        self.assertIn("1 kubectl call(s)", str(raised.exception))
        self.assertIn("kubectl get secret -n kubeagents-system", str(raised.exception))

    def test_a_second_install_shares_the_guard_until_the_last_uninstall(self):
        """unittest runs a module's fixtures again around a suite one of its own
        tests runs; the inner pair must leave the outer guard in force."""
        outer = no_live_kubectl.install()
        inner = no_live_kubectl.install()
        self.assertIs(inner, outer)
        inner.uninstall()
        self.assertEqual(os.environ["KUBECONFIG"], str(outer.kubeconfig))
        self.assertTrue(outer.stand_in.exists())
        outer.uninstall()
        self.assertFalse(outer.stand_in.exists())

    def test_an_uninstall_after_the_last_one_does_nothing(self):
        guard = no_live_kubectl.install()
        guard.uninstall()
        with patch.dict(os.environ, {"PATH": "/changed/after/the/guard"}):
            guard.uninstall()
            self.assertEqual(os.environ["PATH"], "/changed/after/the/guard")

    def test_a_newline_in_an_argument_stays_in_one_recorded_call(self):
        guard = no_live_kubectl.install()
        subprocess.run(["kubectl", "annotate", "deploy/x", "note=first\nsecond"], capture_output=True)
        self.assertEqual(guard.calls(), ["annotate deploy/x note=first\nsecond"])
        with self.assertRaises(AssertionError) as raised:
            guard.uninstall()
        self.assertIn("1 kubectl call(s)", str(raised.exception))

    def test_bytes_that_are_not_utf8_do_not_stop_the_cleanup(self):
        guard = no_live_kubectl.install()
        stand_in = guard.stand_in
        subprocess.run([b"kubectl", b"get", b"\xff\xfe"], capture_output=True)
        with self.assertRaises(AssertionError) as raised:
            guard.uninstall()
        self.assertIn("1 kubectl call(s)", str(raised.exception))
        self.assertFalse(stand_in.exists())

    def test_a_pinned_state_read_does_not_fail_the_module(self):
        guard = no_live_kubectl.install()
        subprocess.run(STATE_READ, capture_output=True, text=True)
        guard.uninstall()


if __name__ == "__main__":
    unittest.main()
