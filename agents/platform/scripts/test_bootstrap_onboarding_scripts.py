"""Unit tests for the no-LLM onboarding cron scripts.

Run: python3 -m unittest agents/platform/scripts/test_bootstrap_onboarding_scripts.py

Covers the deterministic decision + I/O logic of:
  - bootstrap_delivery.py  (no_agent delivery of INVENTORY.md, exactly once)
  - bootstrap_scan_gate.py (wake-gate that stops the scan re-running)

The in-process job removal in bootstrap_delivery._cleanup imports cron.jobs,
which is unavailable here; its import is guarded, so _cleanup degrades to a
no-op for job removal while still marking completion and removing INVENTORY.md.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import bootstrap_delivery  # noqa: E402
import bootstrap_scan_gate  # noqa: E402

INVENTORY = "INVENTORY.md"
ALIGNED = ".user_aligned"
COMPLETED = ".bootstrap_completed"


class DeliveryDecisionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_deliver_when_nothing_present(self):
        self.assertFalse(bootstrap_delivery._should_deliver(self.d))

    def test_no_deliver_when_only_inventory(self):
        (self.d / INVENTORY).write_text("x")
        self.assertFalse(bootstrap_delivery._should_deliver(self.d))

    def test_no_deliver_when_only_aligned(self):
        (self.d / ALIGNED).touch()
        self.assertFalse(bootstrap_delivery._should_deliver(self.d))

    def test_deliver_when_both_present_and_not_completed(self):
        (self.d / INVENTORY).write_text("x")
        (self.d / ALIGNED).touch()
        self.assertTrue(bootstrap_delivery._should_deliver(self.d))

    def test_no_deliver_when_already_completed(self):
        (self.d / INVENTORY).write_text("x")
        (self.d / ALIGNED).touch()
        (self.d / COMPLETED).touch()
        self.assertFalse(bootstrap_delivery._should_deliver(self.d))


class DeliveryMainTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bootstrap_delivery.main(self.d)
        return rc, buf.getvalue()

    def test_silent_when_not_ready(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        self.assertFalse((self.d / COMPLETED).exists())

    def test_emits_verbatim_and_concludes_once(self):
        report = "# GKE Environment Discovery Report\n\n| Cluster | ... |\n"
        (self.d / INVENTORY).write_text(report, encoding="utf-8")
        (self.d / ALIGNED).touch()

        rc, out = self._run()
        self.assertEqual(rc, 0)
        # Delivered verbatim — byte-for-byte, no reformatting.
        self.assertEqual(out, report)
        # Concluded: completion marked, single-use report removed.
        self.assertTrue((self.d / COMPLETED).exists())
        self.assertFalse((self.d / INVENTORY).exists())

    def test_second_run_is_silent(self):
        (self.d / INVENTORY).write_text("report", encoding="utf-8")
        (self.d / ALIGNED).touch()
        self._run()  # first delivery
        rc, out = self._run()  # second tick
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")


class ScanGateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bootstrap_scan_gate.main(self.d)
        return rc, buf.getvalue().strip()

    def test_wakes_when_no_inventory(self):
        self.assertFalse(bootstrap_scan_gate.should_skip(self.d))
        rc, out = self._run()
        self.assertEqual(rc, 0)
        # Must emit a non-empty wake line: empty stdout makes the scheduler
        # skip the agent ("nothing to report"), so discovery would never run.
        self.assertEqual(out, '{"wakeAgent": true}')

    def test_skips_when_inventory_present(self):
        (self.d / INVENTORY).write_text("x")
        self.assertTrue(bootstrap_scan_gate.should_skip(self.d))
        _, out = self._run()
        self.assertEqual(out, '{"wakeAgent": false}')

    def test_skips_when_completed(self):
        # Even after INVENTORY.md is removed at cleanup, completion keeps the
        # scan from re-running.
        (self.d / COMPLETED).touch()
        self.assertTrue(bootstrap_scan_gate.should_skip(self.d))
        _, out = self._run()
        self.assertEqual(out, '{"wakeAgent": false}')


if __name__ == "__main__":
    unittest.main()
