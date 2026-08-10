"""Unit tests for the no-LLM onboarding cron scripts.

Run: python3 -m unittest agents/chat/scripts/test_bootstrap_onboarding_scripts.py

Covers the deterministic decision + I/O logic of:
  - bootstrap_delivery.py  (no_agent delivery of INVENTORY.md, exactly once)
  - bootstrap_scan_gate.py (files the sweep as a kanban task; stops re-filing)

The in-process job removal in bootstrap_delivery._cleanup imports cron.jobs,
which is unavailable here; its import is guarded, so _cleanup degrades to a
no-op for job removal while still archiving INVENTORY.md.

The theme of these tests is that onboarding happens ONCE. Each stage is
therefore checked twice: once for doing its job, and once for refusing to do it
again.
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
DELIVERED = "INVENTORY.delivered.md"
ALIGNED = ".user_aligned"
COMPLETED = ".bootstrap_completed"
SCAN_FILED = ".bootstrap_scan_filed"


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
        # Concluded: completion marked, report moved out of the scan gate's way.
        self.assertTrue((self.d / COMPLETED).exists())
        self.assertFalse((self.d / INVENTORY).exists())

    def test_delivered_report_is_kept_for_resending(self):
        # A fleet-wide sweep is expensive and a chat message is easy to lose.
        # Concluding onboarding must not destroy the only copy of the report.
        report = "# Report\n"
        (self.d / INVENTORY).write_text(report, encoding="utf-8")
        (self.d / ALIGNED).touch()
        self._run()
        self.assertEqual((self.d / DELIVERED).read_text(encoding="utf-8"), report)

    def test_second_run_is_silent(self):
        (self.d / INVENTORY).write_text("report", encoding="utf-8")
        (self.d / ALIGNED).touch()
        self._run()  # first delivery
        rc, out = self._run()  # second tick
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_claim_is_atomic_so_a_racing_run_stays_silent(self):
        """Two runs, one report: only one may reach stdout.

        The scheduled tick and the plugin's trigger_job can overlap. Simulate
        the loser of that race by staging the claim marker as if the winner had
        just taken it, with everything else still saying "ready to deliver".
        """
        (self.d / INVENTORY).write_text("report", encoding="utf-8")
        (self.d / ALIGNED).touch()
        self.assertTrue(bootstrap_delivery._claim_delivery(self.d))  # winner
        self.assertFalse(bootstrap_delivery._claim_delivery(self.d))  # loser

        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")
        # The winner still owns the report: the loser must not archive it.
        self.assertTrue((self.d / INVENTORY).exists())

    def test_unreadable_report_leaves_state_untouched(self):
        # Nothing was delivered, so nothing may be marked delivered — otherwise
        # a transient read error silently costs the user the whole report.
        (self.d / INVENTORY).mkdir()  # a directory: read_text raises OSError
        (self.d / ALIGNED).touch()
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertFalse((self.d / COMPLETED).exists())


class ScanGateTest(unittest.TestCase):
    """The scan gate files the sweep as a kanban task for the `platform` profile.

    The Chat Agent profile this cron runs on holds no terminal/gcloud, so the
    sweep cannot execute here; the gate's whole job is to decide whether to file
    the card. It must stay silent on stdout either way — `deliver: local` plus
    empty output means the scheduler never posts anything for this job.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.filed = []
        self._orig = bootstrap_scan_gate.file_scan_task

        def _fake_file(data_dir):
            self.filed.append(1)
            bootstrap_scan_gate._mark_filed(data_dir, "t_test")
            return "t_test"

        bootstrap_scan_gate.file_scan_task = _fake_file

    def tearDown(self):
        bootstrap_scan_gate.file_scan_task = self._orig
        self._tmp.cleanup()

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bootstrap_scan_gate.main(self.d)
        return rc, buf.getvalue().strip()

    def test_files_task_when_no_inventory(self):
        self.assertFalse(bootstrap_scan_gate.should_skip(self.d))
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.filed), 1)
        self.assertEqual(out, "")  # never speaks to the user

    def test_skips_when_inventory_present(self):
        (self.d / INVENTORY).write_text("x")
        self.assertTrue(bootstrap_scan_gate.should_skip(self.d))
        _, out = self._run()
        self.assertEqual(self.filed, [])
        self.assertEqual(out, "")

    def test_skips_when_completed(self):
        # Even after INVENTORY.md is removed at cleanup, completion keeps the
        # scan from being filed again.
        (self.d / COMPLETED).touch()
        self.assertTrue(bootstrap_scan_gate.should_skip(self.d))
        _, out = self._run()
        self.assertEqual(self.filed, [])
        self.assertEqual(out, "")

    def test_files_only_once_across_ticks(self):
        """The regression this whole marker exists for.

        The job runs every 60 seconds and the sweep takes minutes, so between
        filing the card and the report appearing there are many ticks in which
        neither INVENTORY.md nor .bootstrap_completed exists. Without a marker
        of its own the gate re-files a fleet-wide scan on every one of them.
        """
        for _ in range(5):
            self._run()
        self.assertEqual(len(self.filed), 1)

    def test_filed_marker_records_the_card(self):
        # Written with the card id so an operator debugging a stalled onboarding
        # knows which card to open, not merely that one exists somewhere.
        self._run()
        self.assertIn("t_test", (self.d / SCAN_FILED).read_text(encoding="utf-8"))

    def test_skips_while_sweep_is_in_flight(self):
        (self.d / SCAN_FILED).write_text("task_id=t_test\n")
        self.assertTrue(bootstrap_scan_gate.should_skip(self.d))
        _, out = self._run()
        self.assertEqual(self.filed, [])
        self.assertEqual(out, "")

    def test_no_marker_when_the_board_refuses_the_card(self):
        # A marker written after a failed create would silence discovery
        # forever — the one failure mode worse than repeating it.
        bootstrap_scan_gate.file_scan_task = self._orig
        self._run()  # hermes_cli.kanban is unavailable here, so the create fails
        self.assertFalse((self.d / SCAN_FILED).exists())
        self.assertFalse(bootstrap_scan_gate.should_skip(self.d))  # retries next tick

    def test_card_is_idempotent_and_pins_absolute_output_path(self):
        # The key is the board-side backstop behind the filed marker; the
        # absolute path is what keeps the report in the Chat Agent's home where
        # the delivery job reads it.
        self.assertEqual(bootstrap_scan_gate.SCAN_IDEMPOTENCY_KEY, "bootstrap-inventory-scan")
        self.assertEqual(bootstrap_scan_gate.SCAN_ASSIGNEE, "platform")
        self.assertEqual(bootstrap_scan_gate.INVENTORY_PATH, "/opt/data/INVENTORY.md")
        self.assertIn("/opt/data/INVENTORY.md", bootstrap_scan_gate._task_body())

    def test_body_drives_per_cluster_fan_out_and_covers_management(self):
        """The sweep must scale per cluster and must not leave a management-cluster hole.

        Cluster Agents are pinned read-only to one cluster each, and reconcile
        deliberately creates none for the cluster the pod itself runs on — so the
        management cluster is only covered if platform scans it directly.
        """
        body = bootstrap_scan_gate._task_body()
        self.assertIn(bootstrap_scan_gate.RECONCILE_SCRIPT, body)  # roster first
        self.assertIn("management cluster", body)  # platform covers it itself
        self.assertIn("kanban_create", body)  # one child per cluster
        self.assertIn("parents=", body)  # fan-in collects the results
        self.assertIn("metadata", body)  # structured child results

    def test_body_degrades_when_no_cluster_agents_exist(self):
        # Cluster Agents ship now, but the script is still absent wherever an older
        # image is running or no cluster has an agent yet. The same card must produce a
        # report there rather than fanning out to an empty roster and writing nothing.
        body = bootstrap_scan_gate._task_body()
        self.assertIn("If the script is absent", body)
        self.assertIn("no Cluster Agents", body)

    def test_body_propagates_idempotency_keys_to_the_fan_out(self):
        # The root card is guarded by a marker and a key; the cards it spawns
        # are guarded only by what these instructions tell the worker to set.
        body = bootstrap_scan_gate._task_body()
        self.assertIn(bootstrap_scan_gate.AGGREGATE_IDEMPOTENCY_KEY, body)
        self.assertIn(bootstrap_scan_gate.CLUSTER_IDEMPOTENCY_KEY_PREFIX, body)

    def test_parses_task_id_from_either_response_shape(self):
        # --json is what we ask for, but run_slash hands back stdout and stderr
        # together, and an older board may not support --json on create at all.
        parse = bootstrap_scan_gate._parse_task_id
        self.assertEqual(parse('{"id": "t_abc", "status": "ready"}'), "t_abc")
        self.assertEqual(parse('warning: noise\n{"id": "t_abc"}\n'), "t_abc")
        self.assertEqual(parse("Created t_abc  (ready, assignee=platform)"), "t_abc")
        self.assertIsNone(parse("kanban create: board unavailable"))


if __name__ == "__main__":
    unittest.main()
