"""Unit tests for no_live_gcloud, the guard that keeps a test module off the real gcloud.

Each test installs a guard of its own, so a call the stand-in records here is
the test's subject and not a leak.
"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.testing import no_live_gcloud


class GuardTestCase(unittest.TestCase):
    def install(self):
        guard = no_live_gcloud.install()
        self.addCleanup(self._discard, guard)
        # Before any test here runs gcloud by name: with a broken guard that
        # name is the real gcloud, and this is where the test stops.
        self.assertEqual(shutil.which("gcloud"), str(guard.stand_in))
        return guard

    @staticmethod
    def _discard(guard):
        # A test that has not uninstalled leaves the environment as it found it.
        if os.environ.get("PATH", "").startswith(str(guard.bin_dir)):
            try:
                guard.uninstall()
            except AssertionError:
                pass


class TestStandIn(GuardTestCase):
    def test_the_stand_in_is_the_gcloud_on_path(self):
        guard = self.install()
        self.assertEqual(shutil.which("gcloud"), str(guard.stand_in))
        no_live_gcloud.assert_in_force(self, guard)

    def test_the_stand_in_answers_a_gcloud_process_and_records_it(self):
        guard = self.install()
        done = subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True)
        self.assertEqual(done.returncode, no_live_gcloud.STAND_IN_EXIT_CODE)
        self.assertIn(no_live_gcloud.STAND_IN_MESSAGE, done.stderr)
        self.assertEqual(done.stdout, "")
        self.assertEqual(guard.calls(), ["config get-value project"])

    def test_a_gcloud_run_through_a_shell_reaches_the_stand_in_too(self):
        guard = self.install()
        subprocess.run(["sh", "-c", "gcloud storage cat gs://bucket/object || true"], capture_output=True, text=True)
        self.assertEqual(guard.calls(), ["storage cat gs://bucket/object"])

    def test_an_argument_with_a_newline_is_one_call(self):
        guard = self.install()
        subprocess.run(["gcloud", "projects", "describe", "a\nb"], capture_output=True, text=True)
        self.assertEqual(guard.calls(), ["projects describe a\nb"])

    def test_a_tests_own_gcloud_ahead_on_path_answers_first(self):
        guard = self.install()
        with tempfile.TemporaryDirectory() as tmp:
            own = Path(tmp) / "gcloud"
            own.write_text("#!/bin/sh\necho own\n", encoding="utf-8")
            own.chmod(0o755)
            env = dict(os.environ, PATH=f"{tmp}{os.pathsep}{os.environ['PATH']}")
            done = subprocess.run(["gcloud", "version"], capture_output=True, text=True, env=env)
        self.assertEqual(done.stdout, "own\n")
        self.assertEqual(guard.calls(), [])


class TestUninstall(GuardTestCase):
    def test_uninstall_restores_path_and_removes_the_stand_in(self):
        before = os.environ["PATH"]
        guard = self.install()
        self.assertNotEqual(os.environ["PATH"], before)
        guard.uninstall()
        self.assertEqual(os.environ["PATH"], before)
        self.assertFalse(guard.stand_in.exists())

    def test_uninstall_is_quiet_when_no_test_reached_gcloud(self):
        self.install().uninstall()

    def test_uninstall_fails_and_names_the_calls_a_test_made(self):
        before = os.environ["PATH"]
        guard = self.install()
        subprocess.run(["gcloud", "compute", "addresses", "describe", "forge"], capture_output=True)
        subprocess.run(["gcloud", "storage", "cp", "gs://b/o", "."], capture_output=True)
        with self.assertRaises(AssertionError) as raised:
            guard.uninstall()
        message = str(raised.exception)
        self.assertIn("2 gcloud call(s)", message)
        self.assertIn("gcloud compute addresses describe forge", message)
        self.assertIn("gcloud storage cp gs://b/o .", message)
        # The failure still restores the environment.
        self.assertEqual(os.environ["PATH"], before)

    def test_the_failure_quotes_no_more_than_the_first_calls(self):
        guard = self.install()
        for index in range(no_live_gcloud.REPORTED_CALLS + 2):
            subprocess.run(["gcloud", "call", str(index)], capture_output=True)
        with self.assertRaises(AssertionError) as raised:
            guard.uninstall()
        message = str(raised.exception)
        self.assertIn(f"{no_live_gcloud.REPORTED_CALLS + 2} gcloud call(s)", message)
        self.assertIn(f"gcloud call {no_live_gcloud.REPORTED_CALLS - 1}", message)
        self.assertNotIn(f"gcloud call {no_live_gcloud.REPORTED_CALLS}", message)


if __name__ == "__main__":
    unittest.main()
