"""Unit tests for first_run_audit_gate.py.

Run: python3 -m unittest agents/chat/scripts/test_first_run_audit_gate.py

Covers the deterministic decision + I/O logic of:
  - first_run_audit_gate.py (files first-run audits as kanban tasks; stops re-filing)

The theme of these tests is that first-run audits happen ONCE, and that partial
failures do not write the marker (so the job retries on the next tick).
"""

import json
import os
import sys
import tempfile
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
        """Don't skip if bootstrap complete and no marker."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        self.assertFalse(first_run_audit_gate.should_skip(self.d))


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

    @mock.patch("first_run_audit_gate.file_audit_card")
    def test_files_all_audits_and_writes_marker(self, mock_file):
        """When all cards succeed, write marker with all card IDs."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        
        # Mock successful card creation
        mock_file.side_effect = ["card-1", "card-2", "card-3", "card-4"]
        
        rc = first_run_audit_gate.main(self.d)
        
        self.assertEqual(rc, 0)
        self.assertEqual(mock_file.call_count, 4)
        
        # Marker should exist with all 4 cards
        marker = self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME
        self.assertTrue(marker.exists())
        data = json.loads(marker.read_text())
        self.assertEqual(len(data["cards"]), 4)
        self.assertIn("filed_at", data)

    @mock.patch("first_run_audit_gate.file_audit_card")
    def test_no_marker_when_any_card_fails(self, mock_file):
        """If any card fails, don't write marker (retry on next tick)."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        
        # First 3 succeed, last one fails
        mock_file.side_effect = ["card-1", "card-2", "card-3", None]
        
        rc = first_run_audit_gate.main(self.d)
        
        # Should return error code
        self.assertEqual(rc, 1)
        # Marker should NOT exist
        marker = self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME
        self.assertFalse(marker.exists())

    @mock.patch("first_run_audit_gate.file_audit_card")
    def test_no_marker_when_all_cards_fail(self, mock_file):
        """If all cards fail, don't write marker."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        
        mock_file.return_value = None  # All fail
        
        rc = first_run_audit_gate.main(self.d)
        
        self.assertEqual(rc, 1)
        marker = self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME
        self.assertFalse(marker.exists())

    @mock.patch("first_run_audit_gate.file_audit_card")
    def test_files_only_once_across_ticks(self, mock_file):
        """After marker is written, subsequent ticks are no-ops."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        mock_file.return_value = "card-1"
        
        # First tick - should file
        rc1 = first_run_audit_gate.main(self.d)
        self.assertEqual(rc1, 0)
        call_count_after_first = mock_file.call_count
        
        # Second tick - should skip (marker exists)
        rc2 = first_run_audit_gate.main(self.d)
        self.assertEqual(rc2, 0)
        # No additional calls
        self.assertEqual(mock_file.call_count, call_count_after_first)

    @mock.patch("first_run_audit_gate.file_audit_card")
    def test_marker_records_card_ids(self, mock_file):
        """Marker file contains the filed card IDs for debugging."""
        (self.d / first_run_audit_gate.BOOTSTRAP_COMPLETE_MARKER_NAME).write_text("x")
        mock_file.side_effect = ["waste-card", "security-card", "reliability-card", "capacity-card"]
        
        first_run_audit_gate.main(self.d)
        
        marker = self.d / first_run_audit_gate.FIRST_RUN_FILED_MARKER_NAME
        data = json.loads(marker.read_text())
        
        card_ids = [c["card_id"] for c in data["cards"]]
        self.assertEqual(card_ids, ["waste-card", "security-card", "reliability-card", "capacity-card"])


class DataDirTest(unittest.TestCase):
    """Tests for HERMES_HOME handling."""

    def test_data_dir_defaults_to_opt_data(self):
        """Without HERMES_HOME, defaults to /opt/data."""
        with mock.patch.dict(os.environ, {}, clear=True):
            # Remove HERMES_HOME if present
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
