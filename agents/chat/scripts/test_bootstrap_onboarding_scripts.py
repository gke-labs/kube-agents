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
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_skips_while_prioritization_is_in_flight(self):
        # New window: the sweep is done and the report is not written yet.
        # INVENTORY.md alone no longer covers it, because ranking is its own
        # card and takes its own time.
        (self.d / "INVENTORY.raw.md").write_text("findings")
        self.assertTrue(bootstrap_scan_gate.should_skip(self.d))
        _, out = self._run()
        self.assertEqual(self.filed, [])
        self.assertEqual(out, "")

    def test_body_hands_ranking_to_a_separate_card(self):
        """Ranking must not happen inside the sweep.

        The delivered report has to be produced from the raw findings alone. A
        worker that ranks inline ranks against its own sweep transcript too, so
        the same findings yield a different report depending on how the sweep
        went — which is exactly what a fresh card prevents.
        """
        body = bootstrap_scan_gate._task_body()
        self.assertIn(bootstrap_scan_gate.RAW_INVENTORY_PATH, body)
        self.assertIn(bootstrap_scan_gate.PRIORITIZE_IDEMPOTENCY_KEY, body)
        for path in bootstrap_scan_gate.PRIORITIZE_INSTRUCTIONS_PATHS:
            self.assertIn(path, body)
        self.assertIn("Do not rank the findings yourself", body)

    def test_child_cards_are_pointed_at_the_per_cluster_audit_sop(self):
        """Without the SOP path the child body is written freehand.

        Observed: four per-cluster cards completed in under two minutes each,
        every one with no `metadata` at all, and the fleet report that followed
        named zero problems on a fleet that had them.
        """
        body = bootstrap_scan_gate._task_body()
        for path in bootstrap_scan_gate.CLUSTER_AUDIT_INSTRUCTIONS_PATHS:
            self.assertIn(path, body)

    def test_raw_and_delivered_paths_are_distinct(self):
        # Same file for both would make the delivery job fire on the unranked
        # sweep output — the pre-prioritization behaviour, silently restored.
        self.assertEqual(bootstrap_scan_gate.RAW_INVENTORY_PATH, "/opt/data/INVENTORY.raw.md")
        self.assertNotEqual(
            bootstrap_scan_gate.RAW_INVENTORY_PATH, bootstrap_scan_gate.INVENTORY_PATH
        )

    def test_card_is_idempotent_and_pins_absolute_output_path(self):
        # The key is the board-side backstop behind the filed marker; the
        # absolute path is what keeps the report in the Chat Agent's home where
        # the delivery job reads it.
        self.assertEqual(bootstrap_scan_gate.SCAN_IDEMPOTENCY_KEY, "bootstrap-inventory-scan")
        self.assertEqual(bootstrap_scan_gate.SCAN_ASSIGNEE, "platform")
        self.assertEqual(bootstrap_scan_gate.INVENTORY_PATH, "/opt/data/INVENTORY.md")
        self.assertIn("/opt/data/INVENTORY.md", bootstrap_scan_gate._task_body())

    def test_body_drives_per_cluster_fan_out_and_leaves_no_cluster_uncovered(self):
        """The sweep must scale per cluster and must not leave a hole.

        Cluster Agents are pinned read-only to one cluster each. Which clusters
        get one is reconcile's decision, not this body's, so the body delegates
        every cluster on the roster and audits only what the roster missed.
        """
        body = bootstrap_scan_gate._task_body()
        self.assertIn(bootstrap_scan_gate.RECONCILE_SCRIPT_NAME, body)  # roster first
        self.assertIn("did not cover yourself", body)  # no silent hole
        self.assertIn("kanban_create", body)  # one child per cluster
        # The sweep card waits for its children and synthesizes their results
        # itself; completing on a dispatch receipt is the #1010 defect, and the
        # retired aggregation-card handoff must not creep back into the body.
        self.assertIn("wait for the children", body)
        self.assertIn("kanban_show", body)
        self.assertNotIn("aggregation card", body)
        self.assertIn("metadata", body)  # structured child results

    def test_roster_command_carries_both_fixes(self):
        """A bare `hermes profile list` has two distinct failure modes.

        Without the absolute path it exits 127: the kanban worker's terminal runs
        with a stripped environment where /opt/hermes/.venv/bin is not on PATH,
        though it works fine from an interactive shell.

        Without a pinned HERMES_HOME it is worse than that — it exits 0 and prints
        a roster that is missing profiles. The pin must be unconditional: the
        worker HAS a HERMES_HOME, set to its own profile home, so a defaulted
        expansion (${HERMES_HOME:-...}) keeps the wrong value and reproduces the
        quiet failure. A loud failure gets retried; a quiet wrong answer gets
        believed, and on a multi-cluster fleet it drops clusters from the sweep
        with no trace. Both halves must survive.
        """
        cmd = bootstrap_scan_gate._roster_command()
        self.assertIn("/opt/hermes/.venv/bin/hermes", cmd)
        self.assertTrue(
            cmd.startswith(f"HERMES_HOME={bootstrap_scan_gate._data_dir()} ")
        )
        self.assertNotIn(":-", cmd)  # no defaulted expansion — see docstring
        self.assertIn(cmd, bootstrap_scan_gate._task_body())

    def test_roster_command_tracks_a_custom_agent_home(self):
        """/opt/data is the default data root, not a constant.

        With spec.harness.hermes.agentHome set, this gate's HERMES_HOME is that
        home and the profiles live under it. A literal /opt/data would point
        hermes at a tree with no profiles — the same quiet empty roster the pin
        exists to prevent, one configuration over.
        """
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/var/agent"}):
            cmd = bootstrap_scan_gate._roster_command()
            self.assertTrue(cmd.startswith("HERMES_HOME=/var/agent "))
            self.assertIn(cmd, bootstrap_scan_gate._task_body())

    def test_body_forbids_improvising_around_a_failed_step(self):
        """The 32-call roster loop is what this prevents.

        When the roster command returned 127 the worker did not stop: it tried ls,
        sqlite3, four python sqlite attempts against three databases, five metadata
        server probes, and re-ran the reconcile script five times — half the sweep,
        for an answer ("no cluster agents") that is also the safe default.
        """
        body = bootstrap_scan_gate._task_body()
        self.assertIn("treat its answer as empty", body)
        self.assertIn("do not improvise", body.lower())
        self.assertIn("exactly once", body)

    def test_reconcile_runs_before_the_sweep_is_filed(self):
        """A sweep filed against a not-yet-reconciled roster fans out to nobody.

        `cluster-agent-reconcile` is on `11 * * * *` and this gate is on
        `* * * * *`, so on a fresh install the gate reaches an empty roster up to
        59 minutes before anything populates it — and the marker it writes makes
        that solo sweep the only one that ever runs.
        """
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertTrue(bootstrap_scan_gate.ensure_cluster_agents(self.d))
        self.assertEqual(len(calls), 1)
        # Resolved under the data dir, not a hardcoded /opt/data: `agentHome`
        # moves the tree, and a path that misses silently files a solo sweep.
        self.assertIn(str(self.d / "scripts" / bootstrap_scan_gate.RECONCILE_SCRIPT_NAME), calls[0])

    def test_the_gate_asks_the_reconcile_for_an_exit_code_that_means_something(self):
        """Without `--require-create-pass` the exit code is always 0.

        The script is a cron producer and swallows every failure — a `gcloud
        container clusters list` that 403s is logged and the run exits 0. The gate
        would then reset the attempt count and file a solo sweep that
        `.bootstrap_scan_filed` makes permanent, which is the failure this whole
        arm exists to prevent.
        """
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            bootstrap_scan_gate.ensure_cluster_agents(self.d)
        self.assertIn("--require-create-pass", calls[0])

    def test_a_failed_reconcile_defers_the_sweep_without_marking_it(self):
        # EXIT_CREATE_PASS_SKIPPED: the CREATE direction did not run, so the roster
        # is not reconciled and the sweep must not be filed against it.
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 3, "", "boom")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertFalse(bootstrap_scan_gate.ensure_cluster_agents(self.d))

    def test_reconcile_stops_blocking_onboarding_after_repeated_failure(self):
        """A reconcile that can never succeed must not hold onboarding shut.

        No IAM to list clusters is a permanent condition on some installs. A
        solo sweep is a worse report; no report at all is none.
        """
        stale = time.time() - bootstrap_scan_gate.RECONCILE_GIVE_UP_SECONDS - 1
        (self.d / bootstrap_scan_gate.RECONCILE_ATTEMPTS_MARKER).write_text(
            f"{bootstrap_scan_gate.MAX_RECONCILE_ATTEMPTS}\n{stale}\n"
        )
        with mock.patch.object(Path, "exists", lambda self: True):
            self.assertTrue(bootstrap_scan_gate.ensure_cluster_agents(self.d))

    def test_the_attempt_ceiling_alone_does_not_give_up_in_the_first_minutes(self):
        """The count is exhausted but the streak is young: keep waiting.

        The gate ticks every minute with no backoff, so five attempts is five
        minutes — and IAM propagation on a fresh install routinely takes longer
        than that. Giving up there files the solo sweep that
        ``.bootstrap_scan_filed`` makes permanent.
        """
        (self.d / bootstrap_scan_gate.RECONCILE_ATTEMPTS_MARKER).write_text(
            f"{bootstrap_scan_gate.MAX_RECONCILE_ATTEMPTS}\n{time.time()}\n"
        )

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 3, "", "no IAM to list clusters")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertFalse(bootstrap_scan_gate.ensure_cluster_agents(self.d))

    def test_a_counter_written_before_the_clock_existed_still_gives_up(self):
        # An install upgraded mid-streak carries a single-line marker. It must not
        # win another 30 minutes of waiting from the upgrade alone.
        (self.d / bootstrap_scan_gate.RECONCILE_ATTEMPTS_MARKER).write_text(
            f"{bootstrap_scan_gate.MAX_RECONCILE_ATTEMPTS}\n"
        )
        with mock.patch.object(Path, "exists", lambda self: True):
            self.assertTrue(bootstrap_scan_gate.ensure_cluster_agents(self.d))

    def test_the_first_failure_stamps_the_streak_start(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 3, "", "boom")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            bootstrap_scan_gate.ensure_cluster_agents(self.d)
            first = bootstrap_scan_gate._reconcile_since(self.d)
            self.assertIsNotNone(first)
            # A later tick extends the streak rather than restarting its clock.
            bootstrap_scan_gate.ensure_cluster_agents(self.d)
        self.assertEqual(bootstrap_scan_gate._reconcile_since(self.d), first)
        self.assertEqual(bootstrap_scan_gate._reconcile_attempts(self.d), 2)

    def test_a_reconcile_that_times_out_defers_the_sweep(self):
        # subprocess.run raising is the timeout path; it must read as "not yet",
        # not as "no Cluster Agents exist".
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, bootstrap_scan_gate.RECONCILE_TIMEOUT_SECONDS)

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertFalse(bootstrap_scan_gate.ensure_cluster_agents(self.d))
        self.assertEqual(bootstrap_scan_gate._reconcile_attempts(self.d), 1)

    def test_a_successful_reconcile_clears_the_attempt_count(self):
        (self.d / bootstrap_scan_gate.RECONCILE_ATTEMPTS_MARKER).write_text("3")

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertTrue(bootstrap_scan_gate.ensure_cluster_agents(self.d))
        self.assertEqual(bootstrap_scan_gate._reconcile_attempts(self.d), 0)

    def test_a_concurrent_reconcile_defers_the_sweep(self):
        # The gate fires every minute and a reconcile takes tens of seconds, so overlap
        # is the normal case. Mutual exclusion lives in the reconcile script, which the
        # hourly cron job also runs; the gate only has to read its refusal.
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, bootstrap_scan_gate.RECONCILE_ALREADY_RUNNING, "", "")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertFalse(bootstrap_scan_gate.ensure_cluster_agents(self.d))

    def test_losing_the_race_does_not_spend_an_attempt(self):
        # Contention says nothing about the roster. Counting it would spend the ceiling
        # that exists for a reconcile which genuinely cannot succeed.
        started = time.time()
        bootstrap_scan_gate._record_reconcile_attempt(self.d, 2, started)

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, bootstrap_scan_gate.RECONCILE_ALREADY_RUNNING, "", "")

        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_run), \
                mock.patch.object(Path, "exists", lambda self: True):
            self.assertFalse(bootstrap_scan_gate.ensure_cluster_agents(self.d))
        self.assertEqual(bootstrap_scan_gate._reconcile_attempts(self.d), 2)
        self.assertEqual(bootstrap_scan_gate._reconcile_since(self.d), started)

    def test_body_forbids_creating_profiles_by_hand(self):
        """The regression that made the first roster fix worse than the bug.

        reconcile is permanently prune-only on any deployment where it cannot
        list clusters (it runs with a cwd outside CREDENTIAL_PROXY_WORKSPACE_ROOT
        and the gcloud call 403s), so the roster is always empty. Told that
        reconcile "ensures every managed cluster has an agent", the worker read an
        empty roster as damage and called cluster_agent_profile.py create directly
        — around the management-cluster guard that only lives inside reconcile.
        The next reconcile run pruned it. Create, prune, repeat: arm 1b ran 102
        shell calls against arm 1a's 65.
        """
        body = bootstrap_scan_gate._task_body()
        self.assertIn("do not repair or delete a profile", body)
        self.assertIn("may immediately prune", body)
        self.assertIn("not yours to fix", body)

    def test_the_body_does_not_promise_the_roster_was_reconciled(self):
        # The gate files the card on the give-up path too, after MAX_RECONCILE_ATTEMPTS
        # failures over RECONCILE_GIVE_UP_SECONDS. A body asserting the roster is current
        # is false there, and it tells the worker to trust a roster that may be empty —
        # then `.bootstrap_scan_filed` makes the thin report permanent.
        body = bootstrap_scan_gate._task_body()
        self.assertNotIn("already reconciled", body)
        self.assertNotIn("exited 0", body)
        self.assertIn("may be empty or incomplete", body)
        # The degradation has to reach the user: the report is delivered verbatim.
        self.assertIn("names each one as lacking an agent", body)
        self.assertIn("file it anyway", body)

    def test_body_degrades_when_no_cluster_agents_exist(self):
        # A single-cluster install reconciles to an empty roster, and that is the
        # supported answer. The same card must still produce a report there rather
        # than fanning out to an empty roster and writing nothing.
        body = bootstrap_scan_gate._task_body()
        self.assertIn("there are no Cluster Agents", body)
        self.assertIn("do the whole sweep yourself", body)
        # The workload checks live only in the single-cluster SOP now, so the solo
        # walk has to be sent there or it produces a topology table with empty
        # workload columns — the empty report this card exists to prevent.
        self.assertIn("single-cluster audit SOP", body)
        # Topology is that SOP's Step 2, so a range starting at 3 leaves the fleet
        # table's K8s version, node pool and Workload Identity columns unsourced on
        # the one path where no Cluster Agent supplies them.
        self.assertIn("Steps 2 to 4 of the single-cluster audit SOP", body)

    def test_body_propagates_idempotency_keys_to_the_fan_out(self):
        # The root card is guarded by a marker and a key; the cards it spawns
        # are guarded only by what these instructions tell the worker to set.
        # (The aggregation card's key went with the fan-in shape, #1010: the
        # sweep card now waits for its children and writes the findings itself,
        # so the only spawned cards left are per-cluster and prioritize.)
        body = bootstrap_scan_gate._task_body()
        self.assertIn(bootstrap_scan_gate.CLUSTER_IDEMPOTENCY_KEY_PREFIX, body)
        self.assertIn(bootstrap_scan_gate.PRIORITIZE_IDEMPOTENCY_KEY, body)

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
