"""Unit tests for first_run_audit_gate.py.

Run: python3 -m unittest agents/chat/scripts/test_first_run_audit_gate.py

Covers the deterministic decision + subprocess trigger logic of:
  - first_run_audit_gate.py (triggers first-run audits via `hermes cron run`; stops re-triggering)
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import first_run_audit_gate  # noqa: E402


class ShouldSkipTest(unittest.TestCase):
    """Tests for should_skip() decision logic."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_skip_when_bootstrap_not_complete(self):
        """Skip if INVENTORY.raw.md doesn't exist yet."""
        self.assertTrue(first_run_audit_gate.should_skip(self.d))

    def test_skip_when_already_filed(self):
        """Skip if marker exists (already filed)."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        (self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME).write_text("{}")
        self.assertTrue(first_run_audit_gate.should_skip(self.d))

    def test_no_skip_when_ready(self):
        """Don't skip if bootstrap completed recently and no marker exists."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        self.assertFalse(first_run_audit_gate.should_skip(self.d))

    def test_skip_when_bootstrap_is_stale_on_upgrade(self):
        """Skip on existing installs where INVENTORY.raw.md predates the 2-hour window."""
        bootstrap = self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME
        bootstrap.write_text("x")
        old_time = time.time() - first_run_audit_gate.MAX_BOOTSTRAP_AGE_SECONDS - 60
        os.utime(bootstrap, (old_time, old_time))
        self.assertTrue(first_run_audit_gate.should_skip(self.d))


class HermesBinTest(unittest.TestCase):
    """Tests for hermes_bin() resolution."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_prefers_sibling_next_to_sys_executable(self):
        fake_py = self.d / "python3"
        fake_hermes = self.d / "hermes"
        fake_py.write_text("")
        fake_hermes.write_text("")
        with mock.patch.object(first_run_audit_gate.sys, "executable", str(fake_py)):
            self.assertEqual(first_run_audit_gate.hermes_bin(), fake_hermes)

    def test_falls_back_to_path_when_no_sibling(self):
        fake_py = self.d / "python3"
        fake_py.write_text("")
        with (
            mock.patch.object(first_run_audit_gate.sys, "executable", str(fake_py)),
            mock.patch.object(
                first_run_audit_gate.shutil, "which", return_value="/usr/local/bin/hermes"
            ),
        ):
            self.assertEqual(
                first_run_audit_gate.hermes_bin(), Path("/usr/local/bin/hermes")
            )

    def test_raises_when_missing_everywhere(self):
        fake_py = self.d / "python3"
        fake_py.write_text("")
        with (
            mock.patch.object(first_run_audit_gate.sys, "executable", str(fake_py)),
            mock.patch.object(first_run_audit_gate.shutil, "which", return_value=None),
        ):
            with self.assertRaises(FileNotFoundError):
                first_run_audit_gate.hermes_bin()


class TriggerAuditTest(unittest.TestCase):
    """Tests for trigger_audit() subprocess invocation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    @mock.patch("first_run_audit_gate.subprocess.run")
    @mock.patch(
        "first_run_audit_gate.hermes_bin",
        return_value=Path("/opt/hermes/.venv/bin/hermes"),
    )
    def test_invokes_hermes_cron_run_with_platform_hermes_home(
        self, _mock_bin, mock_run
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="queued\n", stderr=""
        )
        ok = first_run_audit_gate.trigger_audit("compliance-audit", self.d)
        self.assertTrue(ok)
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        self.assertEqual(
            args[0],
            ["/opt/hermes/.venv/bin/hermes", "cron", "run", "compliance-audit"],
        )
        self.assertEqual(
            kwargs["env"]["HERMES_HOME"],
            str(self.d / "profiles" / "platform"),
        )
        self.assertEqual(kwargs["timeout"], 30)

    @mock.patch("first_run_audit_gate.subprocess.run")
    @mock.patch(
        "first_run_audit_gate.hermes_bin",
        return_value=Path("/opt/hermes/.venv/bin/hermes"),
    )
    def test_returns_false_on_nonzero_exit(self, _mock_bin, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no such job"
        )
        self.assertFalse(first_run_audit_gate.trigger_audit("compliance-audit", self.d))

    @mock.patch(
        "first_run_audit_gate.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="hermes", timeout=30),
    )
    @mock.patch(
        "first_run_audit_gate.hermes_bin",
        return_value=Path("/opt/hermes/.venv/bin/hermes"),
    )
    def test_returns_false_on_timeout(self, _mock_bin, _mock_run):
        self.assertFalse(first_run_audit_gate.trigger_audit("compliance-audit", self.d))

    @mock.patch(
        "first_run_audit_gate.hermes_bin",
        side_effect=FileNotFoundError("no hermes"),
    )
    def test_returns_false_when_hermes_missing(self, _mock_bin):
        self.assertFalse(first_run_audit_gate.trigger_audit("compliance-audit", self.d))


class MainTest(unittest.TestCase):
    """Tests for main() entry point."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_silent_when_not_ready(self):
        """Return 0 and do nothing if bootstrap not complete."""
        rc = first_run_audit_gate.main(self.d)
        self.assertEqual(rc, 0)
        self.assertFalse(
            (self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME).exists()
        )

    def test_silent_when_already_filed(self):
        """Return 0 and do nothing if marker already exists."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        (self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME).write_text("{}")
        rc = first_run_audit_gate.main(self.d)
        self.assertEqual(rc, 0)

    @mock.patch("first_run_audit_gate.trigger_audit")
    def test_triggers_all_audits_and_writes_marker(self, mock_trigger):
        """When all 4 cron triggers succeed, write marker with all job IDs."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        mock_trigger.return_value = True

        rc = first_run_audit_gate.main(self.d)

        self.assertEqual(rc, 0)
        self.assertEqual(mock_trigger.call_count, 4)
        called_jobs = [call.args[0] for call in mock_trigger.call_args_list]
        self.assertEqual(
            tuple(called_jobs),
            (
                "fleet-wide-cost-analysis",
                "compliance-audit",
                "obtainability-audit",
                "stockout-prevention",
            ),
        )

        marker = self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME
        self.assertTrue(marker.exists())
        data = json.loads(marker.read_text())
        self.assertEqual(data["jobs"], list(first_run_audit_gate.FIRST_RUN_AUDITS))
        self.assertIn("filed_at", data)

    @mock.patch("first_run_audit_gate.trigger_audit")
    def test_no_marker_when_any_audit_fails(self, mock_trigger):
        """If any trigger fails, don't write marker (retry on next tick)."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        mock_trigger.side_effect = [True, True, True, False]

        rc = first_run_audit_gate.main(self.d)

        self.assertEqual(rc, 1)
        marker = self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME
        self.assertFalse(marker.exists())

    @mock.patch("first_run_audit_gate.trigger_audit")
    def test_no_marker_when_all_audits_fail(self, mock_trigger):
        """If all triggers fail, don't write marker."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        mock_trigger.return_value = False

        rc = first_run_audit_gate.main(self.d)

        self.assertEqual(rc, 1)
        marker = self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME
        self.assertFalse(marker.exists())

    @mock.patch("first_run_audit_gate.trigger_audit")
    def test_files_only_once_across_ticks(self, mock_trigger):
        """After marker is written, subsequent ticks are no-ops."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        mock_trigger.return_value = True

        rc1 = first_run_audit_gate.main(self.d)
        self.assertEqual(rc1, 0)
        self.assertEqual(mock_trigger.call_count, 4)

        rc2 = first_run_audit_gate.main(self.d)
        self.assertEqual(rc2, 0)
        self.assertEqual(mock_trigger.call_count, 4)

    @mock.patch("first_run_audit_gate.trigger_audit")
    def test_rerun_after_deleting_marker(self, mock_trigger):
        """Deleting the marker while INVENTORY.raw.md is fresh triggers a re-run."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        mock_trigger.return_value = True

        self.assertEqual(first_run_audit_gate.main(self.d), 0)
        self.assertEqual(mock_trigger.call_count, 4)

        (self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME).unlink()
        self.assertEqual(first_run_audit_gate.main(self.d), 0)
        self.assertEqual(mock_trigger.call_count, 8)


class DataDirTest(unittest.TestCase):
    """Tests for HERMES_HOME handling."""

    def test_data_dir_defaults_to_opt_data(self):
        """Without HERMES_HOME, defaults to /opt/data."""
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("HERMES_HOME", None)
            d = first_run_audit_gate._data_dir()
            self.assertEqual(d, Path("/opt/data"))

    def test_data_dir_respects_hermes_home(self):
        """HERMES_HOME overrides the default."""
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/custom/path"}):
            d = first_run_audit_gate._data_dir()
            self.assertEqual(d, Path("/custom/path"))


if __name__ == "__main__":
    unittest.main()
