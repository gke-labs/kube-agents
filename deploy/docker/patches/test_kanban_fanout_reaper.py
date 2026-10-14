"""Unit tests for the fan-out child reaper installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import kanban_fanout_reaper as reaper

# The #2978 shape: build 2110122651069452288, platform card t_89bc86c4 fanned
# out to seeded-b (t_26cc6001), which waited for a workload that wasn't there.
PARENT, CHILD, PERSON = "t_89bc86c4", "t_26cc6001", "t_person01"
HOST = "agent-pod:"
NOW = 1_800_000_000
OVER = reaper.FANOUT_CHILD_MAX_SECONDS + 60

SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT, worker_pid INTEGER,
                    claim_lock TEXT, started_at INTEGER, current_run_id INTEGER,
                    last_heartbeat_at INTEGER, assignee TEXT, result TEXT);
CREATE TABLE task_links (parent_id TEXT NOT NULL, child_id TEXT NOT NULL);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, started_at INTEGER);
CREATE TABLE kanban_worker_children (child_id TEXT PRIMARY KEY, creator_id TEXT NOT NULL,
                                     created_at INTEGER NOT NULL);
"""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(str(Path(tempfile.mkdtemp()) / "kanban.db"))
        self.conn.executescript(SCHEMA)
        self.killed, self.completed = [], []
        self.addCleanup(self.conn.close)

    def card(self, tid, title, *, started_ago, child=True, lock=HOST + "1",
             status="running", run_started_ago=None, pid=4242, assignee=None,
             creator=PARENT, result=None):
        if assignee is None:
            assignee = f"cluster-kube-agents-evals-13-{title}-us-central1-a"
        run = len(self.conn.execute("SELECT id FROM task_runs").fetchall()) + 1
        self.conn.execute(
            "INSERT INTO task_runs VALUES (?, ?, ?)",
            (run, tid, None if run_started_ago is None and started_ago is None
             else NOW - (run_started_ago if run_started_ago is not None else started_ago)),
        )
        self.conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, title, status, pid, lock, NOW - (started_ago or 0), run, NOW, assignee, result),
        )
        if child:
            self.conn.execute(
                "INSERT INTO kanban_worker_children VALUES (?, ?, ?)", (tid, creator, NOW))
        self.conn.commit()

    def sibling(self):
        """The fan-out's other per-cluster card, inside the bound."""
        self.card("t_seeded_c", "seeded-c", started_ago=60)

    def reap(self, complete_ok=True):
        def complete(conn, tid, text, payload):
            self.completed.append((tid, text))
            return complete_ok
        return reaper.reap_overdue_fanout_children(
            self.conn, now=NOW, host_prefix=HOST,
            kill=self.killed.append, complete=complete)


class TestTheBound(Fixture):
    def test_an_overdue_child_with_a_fresh_heartbeat_is_settled(self):
        self.card(CHILD, "seeded-b", started_ago=OVER)
        self.sibling()
        self.assertEqual(self.reap(), [CHILD])
        self.assertEqual(self.killed, [4242])
        tid, text = self.completed[0]
        self.assertEqual(tid, CHILD)
        self.assertIn("Did not finish within 30 minutes on seeded-b", text)
        self.assertIn("unchecked, not as clear", text)

    def test_a_person_filed_card_of_the_same_age_is_left_alone(self):
        self.card(PERSON, "a long investigation", started_ago=OVER, child=False)
        self.assertEqual(self.reap(), [])
        self.assertEqual((self.killed, self.completed), ([], []))

    def test_a_child_inside_the_bound_is_left_alone(self):
        self.card(CHILD, "seeded-b", started_ago=reaper.FANOUT_CHILD_MAX_SECONDS - 60)
        self.assertEqual(self.reap(), [])

    def test_runtime_is_measured_from_the_active_run(self):
        """A retried child's first start is old; the bound is per attempt."""
        self.card(CHILD, "seeded-b", started_ago=OVER, run_started_ago=60)
        self.assertEqual(self.reap(), [])

    def test_a_child_that_is_not_running_is_left_alone(self):
        self.card(CHILD, "seeded-b", started_ago=OVER, status="blocked")
        self.assertEqual(self.reap(), [])

    def test_another_hosts_worker_is_left_to_its_own_dispatcher(self):
        self.card(CHILD, "seeded-b", started_ago=OVER, lock="other-pod:1")
        self.assertEqual(self.reap(), [])
        self.assertEqual(self.killed, [])

    def test_a_card_with_no_worker_pid_is_still_settled(self):
        self.card(CHILD, "seeded-b", started_ago=OVER, pid=None)
        self.sibling()
        self.assertEqual(self.reap(), [CHILD])
        self.assertEqual(self.killed, [])

    def test_a_refused_completion_is_not_reported_as_settled(self):
        self.card(CHILD, "seeded-b", started_ago=OVER)
        self.sibling()
        self.assertEqual(self.reap(complete_ok=False), [])

    def test_one_failing_card_does_not_stop_the_rest(self):
        self.card(CHILD, "seeded-b", started_ago=OVER)
        self.card("t_seeded_d", "seeded-d", started_ago=OVER)

        def kill(pid):
            if not self.killed:
                self.killed.append("boom")
                raise RuntimeError("kill failed")
            self.killed.append(pid)

        settled = reaper.reap_overdue_fanout_children(
            self.conn, now=NOW, host_prefix=HOST, kill=kill,
            complete=lambda conn, tid, text, payload: True)
        self.assertEqual(len(settled), 1)


class TestOnlyPerClusterFanout(Fixture):
    """The reaper's scope: per-cluster cards a worker fanned out, nothing else."""

    def test_a_single_delegation_is_not_a_fanout(self):
        self.card(CHILD, "seeded-b", started_ago=OVER)
        self.assertEqual(self.reap(), [])

    def test_a_follow_up_queued_with_parents_is_left_alone(self):
        self.card(CHILD, "seeded-b", started_ago=OVER)
        self.sibling()
        self.conn.execute("INSERT INTO task_links VALUES (?, ?)", (PARENT, CHILD))
        self.assertEqual(self.reap(), [])

    def test_a_card_for_a_non_cluster_agent_is_left_alone(self):
        self.card("t_audit", "Run the compliance audit", started_ago=OVER, assignee="platform")
        self.card("t_audit2", "Run the upgrade audit", started_ago=OVER, assignee="platform")
        self.assertEqual(self.reap(), [])

    def test_siblings_from_another_creator_do_not_count(self):
        self.card(CHILD, "seeded-b", started_ago=OVER)
        self.card("t_other", "seeded-c", started_ago=60, creator="t_someone_else")
        self.assertEqual(self.reap(), [])

    def test_a_saved_draft_is_kept_in_the_settle_text(self):
        self.card(CHILD, "seeded-b", started_ago=OVER, result="checkout-gateway: not found yet")
        self.sibling()
        self.reap()
        self.assertIn("checkout-gateway: not found yet", self.completed[0][1])


class TestFailOpen(Fixture):
    def test_a_board_without_the_attribution_table_reaps_nothing(self):
        self.conn.execute("DROP TABLE kanban_worker_children")
        self.assertEqual(self.reap(), [])

    def test_a_missing_host_prefix_reaps_nothing(self):
        self.card(CHILD, "seeded-b", started_ago=OVER)

        def boom():
            raise RuntimeError("no claimer id")

        original = reaper._default_host_prefix
        reaper._default_host_prefix = boom
        try:
            self.assertEqual(reaper.reap_overdue_fanout_children(
                self.conn, now=NOW, kill=self.killed.append,
                complete=lambda c, t, x, p: True), [])
        finally:
            reaper._default_host_prefix = original

    def test_settle_text_names_this_cluster_when_untitled(self):
        self.assertIn("on this cluster", reaper.settle_text(""))


class TestTheApplier(unittest.TestCase):
    def setUp(self):
        import apply_kanban_fanout_reaper as applier

        self.applier = applier
        self.tmp = Path(tempfile.mkdtemp())
        self.target = self.tmp / applier.RELATIVE
        self.target.parent.mkdir(parents=True)
        self.target.write_text(
            "def _run_reclaim_phase(conn, result):\n"
            "    result.crashed = detect_crashed_workers(conn)\n"
            + applier.RECLAIM_ANCHOR
            + "    result.promoted = _kb.recompute_ready(conn)\n",
            encoding="utf-8",
        )

    def test_the_hook_lands_after_runtime_enforcement_and_before_promotion(self):
        import ast

        self.applier.apply(self.tmp)
        out = self.target.read_text(encoding="utf-8")
        self.assertLess(out.index("enforce_max_runtime(conn)"), out.index("_kanban_reap_overdue_fanout(conn)"))
        self.assertLess(out.index("_kanban_reap_overdue_fanout(conn)"), out.index("recompute_ready"))
        self.assertIn("from tools.kanban_fanout_reaper import", out)
        ast.parse(out)

    def test_a_second_run_refuses(self):
        self.applier.apply(self.tmp)
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)

    def test_a_missing_anchor_fails_the_build(self):
        self.target.write_text("def _run_reclaim_phase(conn, result):\n    pass\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.applier.apply(self.tmp)


if __name__ == "__main__":
    unittest.main()
