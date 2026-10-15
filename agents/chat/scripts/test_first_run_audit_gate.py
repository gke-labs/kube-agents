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
# oobe imports bootstrap_delivery, which imports sandbox_exec from agents/platform/scripts.
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "platform" / "scripts"))

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
    def test_partial_failure_persists_succeeded_jobs_and_retries_only_failed(
        self, mock_trigger
    ):
        """Partial failure records succeeded IDs and retries only the failed ID next tick."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        mock_trigger.side_effect = [True, True, True, False, True]

        rc1 = first_run_audit_gate.main(self.d)
        self.assertEqual(rc1, 1)

        marker = self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME
        self.assertTrue(marker.exists())
        data1 = json.loads(marker.read_text())
        self.assertEqual(
            data1["jobs"],
            [
                "fleet-wide-cost-analysis",
                "compliance-audit",
                "obtainability-audit",
            ],
        )
        self.assertFalse(first_run_audit_gate.should_skip(self.d))

        rc2 = first_run_audit_gate.main(self.d)
        self.assertEqual(rc2, 0)
        self.assertEqual(mock_trigger.call_count, 5)
        self.assertEqual(mock_trigger.call_args_list[-1].args[0], "stockout-prevention")
        data2 = json.loads(marker.read_text())
        self.assertEqual(data2["jobs"], list(first_run_audit_gate.FIRST_RUN_AUDITS))
        self.assertTrue(first_run_audit_gate.should_skip(self.d))

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

    @mock.patch("first_run_audit_gate.trigger_audit")
    def test_ready_via_settled_bootstrap_scan_when_inventory_on_sandbox(
        self, mock_trigger
    ):
        """When INVENTORY.raw.md is on the sandbox pod, settled scan markers on gateway satisfy readiness."""
        now = 1_000_000.0
        (self.d / first_run_audit_gate.oobe.SCAN_FILED_MARKER).write_text(
            f"task_id=t_sweep\nfiled_at={int(now - 300)}\n", encoding="utf-8"
        )
        (
            self.d / first_run_audit_gate.oobe.bootstrap_handoff.HANDOFF_MARKER
        ).write_text(
            f"sweep=t_sweep\ntask_id=none\nfiled_at={int(now - 300)}\n",
            encoding="utf-8",
        )
        mock_trigger.return_value = True

        self.assertFalse(first_run_audit_gate.should_skip(self.d, now=now))
        self.assertEqual(first_run_audit_gate.main(self.d, now=now), 0)
        self.assertEqual(mock_trigger.call_count, 4)

    @mock.patch("first_run_audit_gate.trigger_audit")
    def test_yields_to_oobe_when_oobe_enabled_or_marker_present(self, mock_trigger):
        """Yields to oobe when oobe is enabled on the chat roster or .oobe_audits_fired is present."""
        now = 1_000_000.0
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        (self.d / first_run_audit_gate.oobe.SCAN_FILED_MARKER).write_text(
            f"task_id=t_sweep\nfiled_at={int(now - 60)}\n", encoding="utf-8"
        )
        cron_dir = self.d / "cron"
        cron_dir.mkdir(parents=True, exist_ok=True)
        (cron_dir / "jobs.json").write_text(
            json.dumps({"jobs": [{"id": "oobe", "enabled": True}]}), encoding="utf-8"
        )

        self.assertTrue(first_run_audit_gate.should_skip(self.d, now=now))
        self.assertEqual(first_run_audit_gate.main(self.d, now=now), 0)
        mock_trigger.assert_not_called()

        # Once oobe finishes and removes itself from jobs.json, .oobe_audits_fired has done=True:
        # first_run_audit_gate yields and retires itself.
        (cron_dir / "jobs.json").write_text(json.dumps({"jobs": []}), encoding="utf-8")
        (self.d / first_run_audit_gate.oobe.AUDITS_MARKER).write_text(
            json.dumps({"done": True, "fired": list(first_run_audit_gate.FIRST_RUN_AUDITS)}),
            encoding="utf-8",
        )
        with mock.patch("first_run_audit_gate.retire") as mock_retire:
            self.assertEqual(first_run_audit_gate.main(self.d, now=now), 0)
            mock_retire.assert_called_once()
        mock_trigger.assert_not_called()

    @mock.patch("first_run_audit_gate.trigger_audit")
    def test_manual_inventory_refresh_overrides_completed_oobe(self, mock_trigger):
        """Touching INVENTORY.raw.md after .oobe_audits_fired completes triggers a manual re-run."""
        oobe_marker = self.d / first_run_audit_gate.oobe.AUDITS_MARKER
        oobe_marker.write_text(
            json.dumps({"done": True, "fired": list(first_run_audit_gate.FIRST_RUN_AUDITS)}),
            encoding="utf-8",
        )
        old_mtime = time.time() - 120
        os.utime(oobe_marker, (old_mtime, old_mtime))

        bootstrap = self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME
        bootstrap.write_text("refreshed")
        mock_trigger.return_value = True

        self.assertFalse(first_run_audit_gate.should_skip(self.d))
        self.assertEqual(first_run_audit_gate.main(self.d), 0)
        self.assertEqual(mock_trigger.call_count, 4)

    @mock.patch("first_run_audit_gate.trigger_audit")
    def test_honours_platform_roster_holds_and_in_flight_audits(self, mock_trigger):
        """Disabled or paused audits on the Platform Agent roster are skipped, and in-flight runs wait."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        platform_cron = self.d / "profiles" / "platform" / "cron"
        platform_cron.mkdir(parents=True, exist_ok=True)
        (platform_cron / "jobs.json").write_text(
            json.dumps(
                {
                    "jobs": [
                        {"id": "fleet-wide-cost-analysis", "enabled": True},
                        {"id": "compliance-audit", "enabled": False},
                        {
                            "id": "obtainability-audit",
                            "enabled": True,
                            "state": "paused",
                        },
                        {"id": "stockout-prevention", "enabled": True},
                    ]
                }
            ),
            encoding="utf-8",
        )
        mock_trigger.return_value = True

        with mock.patch.object(
            first_run_audit_gate.oobe,
            "audits_in_flight",
            return_value={"fleet-wide-cost-analysis"},
        ):
            self.assertEqual(first_run_audit_gate.main(self.d), 0)
            mock_trigger.assert_not_called()

        self.assertEqual(first_run_audit_gate.main(self.d), 0)
        called_jobs = [call.args[0] for call in mock_trigger.call_args_list]
        self.assertEqual(
            called_jobs, ["fleet-wide-cost-analysis", "stockout-prevention"]
        )

    def test_marker_written_atomically_before_trigger_and_corrupt_marker_recovered(
        self,
    ):
        """Each job ID is persisted atomically in .first-run-audits-filed before trigger_audit runs."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        marker = self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME
        marker.write_text("{corrupt json", encoding="utf-8")

        # Corrupt marker is treated as empty rather than all-done
        self.assertEqual(first_run_audit_gate._already_triggered_jobs(self.d), [])

        observed_on_disk = []

        def check_pre_recorded(job_id, _data_dir):
            on_disk = json.loads(marker.read_text(encoding="utf-8"))["jobs"]
            self.assertIn(job_id, on_disk)
            observed_on_disk.append(job_id)
            return True

        with mock.patch(
            "first_run_audit_gate.trigger_audit", side_effect=check_pre_recorded
        ):
            self.assertEqual(first_run_audit_gate.main(self.d), 0)

        self.assertEqual(
            observed_on_disk, list(first_run_audit_gate.FIRST_RUN_AUDITS)
        )
        self.assertEqual(sorted(p.name for p in self.d.glob("*.tmp*")), [])

    def test_retires_when_done_or_stale_and_seeded_in_entrypoint(self):
        """retire() removes first-run-quick-value-audit and docker-entrypoint.sh seeds it in ASSUME_RETIRED."""
        import types

        removed = []
        jobs_mod = types.ModuleType("cron.jobs")
        jobs_mod.remove_job = removed.append
        with mock.patch.dict(
            sys.modules, {"cron": types.ModuleType("cron"), "cron.jobs": jobs_mod}
        ):
            first_run_audit_gate.retire()
        self.assertEqual(removed, [first_run_audit_gate.FIRST_RUN_JOB_ID])

        entrypoint = (
            Path(__file__).resolve().parents[3]
            / "deploy"
            / "shared"
            / "docker-entrypoint.sh"
        ).read_text(encoding="utf-8")
        seeded = next(
            line
            for line in entrypoint.splitlines()
            if line.strip().startswith('ASSUME_RETIRED="bootstrap')
        )
        self.assertIn(
            first_run_audit_gate.FIRST_RUN_JOB_ID,
            seeded.split('"')[1].split(","),
        )


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
