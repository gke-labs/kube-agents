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
import re
import shlex
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.absolute()))
# The gate imports cluster_agent_profile, which the image copies beside it from here.
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "platform" / "scripts"))

import bootstrap_delivery  # noqa: E402
import bootstrap_scan_gate  # noqa: E402
import cluster_agent_profile  # noqa: E402

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
        # Where the pod keeps the profiles, relative to the data dir the gate is given.
        profiles = mock.patch.object(cluster_agent_profile, "PROFILES_BASE", self.d / "profiles")
        profiles.start()
        self.addCleanup(profiles.stop)

    def tearDown(self):
        bootstrap_scan_gate.file_scan_task = self._orig
        self._tmp.cleanup()

    def _run(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = bootstrap_scan_gate.main(self.d)
        return rc, buf.getvalue().strip()

    def _cluster_agent(self, project, cluster, location):
        # Named and stamped by the functions create_profile uses, so the fixture
        # carries the identity block the reconcile actually writes.
        name = cluster_agent_profile.profile_name(project, cluster, location)
        home = cluster_agent_profile.profile_home(name)
        home.mkdir(parents=True)
        cluster_agent_profile._inject_cluster_identity(home, project, cluster, location)
        return name

    @staticmethod
    def _step_2(body):
        return body[body.index("**Step 2") : body.index("**Step 3")]

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

    def test_step_2_lists_one_exact_call_per_cluster_agent(self):
        """The gate reads the roster because the sweep's worker cannot (#1872).

        The worker's terminal runs in the shell sandbox, which has no `hermes`,
        and whose /opt/data/profiles is a mirror without any config.yaml. Sent to
        read the roster there, the worker blocked on 0.6.0; on main it listed the
        mirror's directories and fanned out from that.
        """
        short = self._cluster_agent("proj", "prod", "us-east4")
        # Long enough that profile_name() truncates and hashes it: the assignee is
        # the profile name, and the key comes from the stamped identity.
        hashed = self._cluster_agent(
            "proj", "a-cluster-name-long-enough-to-be-hashed", "us-central1-a"
        )
        self.assertNotIn("us-central1-a", hashed)
        step2 = self._step_2(bootstrap_scan_gate._task_body())
        prefix = bootstrap_scan_gate.CLUSTER_IDEMPOTENCY_KEY_PREFIX
        self.assertIn(
            f"kanban_create(assignee='{short}', idempotency_key='{prefix}proj-prod-us-east4'", step2
        )
        self.assertIn(
            f"kanban_create(assignee='{hashed}', "
            f"idempotency_key='{prefix}proj-a-cluster-name-long-enough-to-be-hashed-us-central1-a'",
            step2,
        )
        self.assertEqual(step2.count("kanban_create("), 2)
        self.assertNotIn("/opt/hermes", step2)
        self.assertNotIn("profile list", step2)

    def test_same_named_clusters_in_two_projects_get_distinct_keys(self):
        # The board matches an idempotency key alone, whatever the assignee, so a
        # shared key hands the second Cluster Agent the first one's card.
        self._cluster_agent("proj-a", "prod", "us-central1")
        self._cluster_agent("proj-b", "prod", "us-central1")
        step2 = self._step_2(bootstrap_scan_gate._task_body())
        keys = re.findall(r"idempotency_key='([^']+)'", step2)
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)

    def test_step_2_leaves_out_what_is_not_a_cluster_agent(self):
        # `default` and `platform` share the directory; stamped here so that only the
        # reserved-name rule can be what drops them. An unstamped profile has no
        # cluster to key its card by, and the reconcile skips it for the same reason.
        for reserved, cluster in (("default", "a"), ("platform", "b")):
            home = self.d / "profiles" / reserved
            home.mkdir(parents=True)
            cluster_agent_profile._inject_cluster_identity(home, "p", cluster, "l")
        (self.d / "profiles" / "cluster-p-unstamped-l").mkdir()
        step2 = self._step_2(bootstrap_scan_gate._task_body())
        self.assertNotIn("kanban_create(", step2)
        self.assertIn("    (none)", step2)
        self.assertIn("If no calls are listed above", step2)

    def test_the_fan_out_calls_carry_no_parents(self):
        # A card whose parent is the sweep card cannot start until the sweep card
        # completes, so the #1174 guard lets the sweep card complete over it: the
        # sweep closed on a dispatch receipt and nothing read the audits (#1872).
        self._cluster_agent("proj", "prod", "us-east4")
        step2 = self._step_2(bootstrap_scan_gate._task_body())
        calls = [line for line in step2.splitlines() if "kanban_create(" in line]
        self.assertEqual(len(calls), 1)
        self.assertNotIn("parents", calls[0])
        self.assertIn("Pass no `parents`", step2)

    def test_an_unreadable_roster_files_the_solo_sweep(self):
        # A scripts directory without cluster_agent_profile must not fail the cron
        # run. The card then reads as having no Cluster Agents, the same answer the
        # gate gives when the reconcile script itself is absent.
        with mock.patch.dict(sys.modules, {"cluster_agent_profile": None}), \
                contextlib.redirect_stderr(io.StringIO()):
            step2 = self._step_2(bootstrap_scan_gate._task_body())
        self.assertIn("    (none)", step2)

    def test_an_unreadable_profile_does_not_drop_the_others(self):
        # A config.yaml that parses to a list makes read_cluster_identity raise.
        good = self._cluster_agent("proj", "prod", "us-east4")
        bad = cluster_agent_profile.profile_home("cluster-broken")
        bad.mkdir(parents=True)
        (bad / "config.yaml").write_text("- a\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            step2 = self._step_2(bootstrap_scan_gate._task_body())
        calls = [line for line in step2.splitlines() if "kanban_create(" in line]
        self.assertEqual(len(calls), 1)
        self.assertIn(f"assignee='{good}'", calls[0])
        self.assertIn("cluster-broken", stderr.getvalue())

    def test_the_card_lists_the_roster_the_reconcile_left(self):
        """The roster is read after the gate's own reconcile, not before it.

        On a fresh install no profile exists until that reconcile creates it, so
        a list taken any earlier is empty and the sweep fans out to nobody.
        """
        bootstrap_scan_gate.file_scan_task = self._orig
        script = self.d / "scripts" / bootstrap_scan_gate.RECONCILE_SCRIPT_NAME
        script.parent.mkdir()
        script.touch()
        created = []

        def fake_reconcile(cmd, **kwargs):
            created.append(self._cluster_agent("proj", "prod", "us-east4"))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        filed = []
        kanban = types.ModuleType("hermes_cli.kanban")
        kanban.run_slash = lambda cmd: filed.append(cmd) or '{"id": "t_real"}'
        modules = {"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.kanban": kanban}
        with mock.patch.object(bootstrap_scan_gate.subprocess, "run", fake_reconcile), \
                mock.patch.dict(sys.modules, modules), \
                contextlib.redirect_stderr(io.StringIO()):
            self._run()
        self.assertEqual(len(filed), 1)
        args = shlex.split(filed[0])
        self.assertIn(f"kanban_create(assignee='{created[0]}'", args[args.index("--body") + 1])

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
        self._cluster_agent("proj", "prod", "us-east4")
        body = bootstrap_scan_gate._task_body()
        self.assertIn(f"{bootstrap_scan_gate.CLUSTER_IDEMPOTENCY_KEY_PREFIX}proj-prod-us-east4", body)
        self.assertIn(bootstrap_scan_gate.PRIORITIZE_IDEMPOTENCY_KEY, body)

    def test_parses_task_id_from_either_response_shape(self):
        # --json is what we ask for, but run_slash hands back stdout and stderr
        # together, and an older board may not support --json on create at all.
        parse = bootstrap_scan_gate._parse_task_id
        self.assertEqual(parse('{"id": "t_abc", "status": "ready"}'), "t_abc")
        self.assertEqual(parse('warning: noise\n{"id": "t_abc"}\n'), "t_abc")
        self.assertEqual(parse("Created t_abc  (ready, assignee=platform)"), "t_abc")
        self.assertIsNone(parse("kanban create: board unavailable"))


class ScopeGapParagraphTest(unittest.TestCase):
    """The sweep is told which projects in scope the reconcile could not list."""

    def _with_snapshot(self, snapshot):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        data_dir = Path(tmp.name)
        if snapshot is not None:
            (data_dir / bootstrap_scan_gate.SCOPE_SNAPSHOT_NAME).write_text(snapshot, encoding="utf-8")
        return data_dir

    def test_no_snapshot_names_nothing(self):
        self.assertEqual(bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot(None)), "")

    def test_an_unreadable_snapshot_names_nothing(self):
        self.assertEqual(bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot("{nope")), "")

    def test_the_task_body_speaks_of_one_project_unless_the_snapshot_names_more(self):
        # An install with no scope renders the prompt it rendered before scopes existed.
        data_dir = self._with_snapshot(None)
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir):
            body = bootstrap_scan_gate._task_body()
        self.assertIn("Audit every cluster the project has", body)
        self.assertIn("If you cannot list the project's clusters at all", body)
        self.assertIn("holds the `RECONCILE_EXCLUDE` opt-out and the create/prune rules", body)
        self.assertNotIn("projects in scope", body)
        self.assertNotIn("the scope and its exclusions", body)
        data_dir = self._with_snapshot('{"projects": [{"id": "mgmt", "outcome": "ok"}, {"id": "other", "outcome": "ok"}]}')
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir):
            body = bootstrap_scan_gate._task_body()
        self.assertIn("Audit every cluster the projects in scope have", body)
        self.assertIn("If you cannot list a project's clusters at all", body)
        self.assertIn("holds the scope and its exclusions and the create/prune rules", body)

    def test_the_paragraph_names_the_container_and_counts_members_past_a_limit(self):
        import json as _json
        rows = [{"id": "mgmt", "outcome": "ok"}] + [{"id": f"p-{i:03d}", "outcome": "over-cap", "via": ["folders/9"]} for i in range(25)]
        snap = _json.dumps({"projects": rows, "containers": [{"id": "folders/9", "outcome": "over-cap", "projects": 25}]})
        paragraph = bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot(snap))
        # Over-cap is a successful lookup past the cap, named as such, never as unresolved.
        self.assertNotIn("could not resolve", paragraph)
        self.assertIn("It resolved `folders/9` (25 project(s)) past its listing cap", paragraph)
        self.assertIn("name the container as over the cap", paragraph)
        self.assertEqual(paragraph.count("`p-"), bootstrap_scan_gate.SCOPE_GAP_NAMED_LIMIT)
        self.assertIn(f"and {25 - bootstrap_scan_gate.SCOPE_GAP_NAMED_LIMIT} more", paragraph)
        self.assertLess(len(paragraph), 2500)

    def test_a_declared_container_makes_the_prompt_multi_project_even_with_one_row(self):
        snap = '{"projects": [{"id": "mgmt", "outcome": "ok"}], "containers": [{"id": "folders/9", "outcome": "unreachable", "projects": 0}]}'
        data_dir = self._with_snapshot(snap)
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir):
            body = bootstrap_scan_gate._task_body()
        self.assertIn("Audit every cluster the projects in scope have", body)
        self.assertIn("could not resolve `folders/9`", body)

    def test_an_unresolved_container_with_no_members_is_still_named(self):
        # First run under a declaration whose folder could not be resolved: no member rows,
        # but a whole folder is missing from the roster and the sweep must hear it.
        snap = '{"projects": [{"id": "mgmt", "outcome": "ok"}, {"id": "p2", "outcome": "ok"}], "containers": [{"id": "folders/9", "outcome": "unreachable", "projects": 0}]}'
        paragraph = bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot(snap))
        self.assertIn("could not resolve `folders/9` (unreachable, 0 project(s) carried)", paragraph)
        self.assertIn("Every project it did resolve was listed.", paragraph)
        # And a single-project install with a resolved container and nothing unlisted stays silent.
        snap = '{"projects": [{"id": "mgmt", "outcome": "ok"}], "containers": [{"id": "folders/9", "outcome": "ok", "projects": 0}]}'
        self.assertEqual(bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot(snap)), "")

    def test_a_single_project_install_gets_no_paragraph_whatever_its_outcome(self):
        # The give-up path on a scope-less install: one project, not ok. The prompt stays main's.
        data_dir = self._with_snapshot('{"projects": [{"id": "mgmt", "outcome": "unreachable"}]}')
        self.assertEqual(bootstrap_scan_gate._scope_gap_paragraph(data_dir), "")
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir):
            body = bootstrap_scan_gate._task_body()
        self.assertNotIn("projects in scope", body)
        self.assertIn("Audit every cluster the project has", body)

    def test_a_snapshot_whose_projects_is_not_a_list_names_nothing(self):
        for body in ('{"projects": null}', '{"projects": 3}', '{"projects": "x"}', '[1]'):
            self.assertEqual(bootstrap_scan_gate._scope_gap_paragraph(self._with_snapshot(body)), "", body)

    def test_all_ok_names_nothing(self):
        snap = '{"projects": [{"id": "a", "outcome": "ok"}, {"id": "b", "outcome": "ok"}]}'
        self.assertEqual(bootstrap_scan_gate._unlisted_projects(self._with_snapshot(snap)), [])

    def test_unlisted_projects_are_named_with_their_outcome_in_sorted_order(self):
        snap = ('{"projects": [{"id": "zeta", "outcome": "denied"}, {"id": "ok-one", "outcome": "ok"},'
                ' {"id": "alpha", "outcome": "unreachable"}]}')
        data_dir = self._with_snapshot(snap)
        self.assertEqual(bootstrap_scan_gate._unlisted_projects(data_dir),
                         [("alpha", "unreachable"), ("zeta", "denied")])
        paragraph = bootstrap_scan_gate._scope_gap_paragraph(data_dir)
        self.assertIn("`alpha` (unreachable), `zeta` (denied)", paragraph)
        self.assertIn("not fully covered", paragraph)

    def test_the_task_body_carries_the_paragraph_when_a_project_is_unlisted(self):
        snap = '{"projects": [{"id": "mgmt", "outcome": "ok"}, {"id": "locked", "outcome": "denied"}]}'
        data_dir = self._with_snapshot(snap)
        with mock.patch.object(bootstrap_scan_gate, "_data_dir", return_value=data_dir):
            body = bootstrap_scan_gate._task_body()
        self.assertIn("`locked` (denied)", body)
        # It sits between the roster caveat and Step 2, where the fan-out reads it.
        self.assertLess(body.index("`locked` (denied)"), body.index("**Step 2"))


if __name__ == "__main__":
    unittest.main()
