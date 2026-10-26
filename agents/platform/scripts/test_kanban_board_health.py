"""Unit tests for kanban_board_health.py — the standing board-health check.

Run: python3 -m unittest agents/platform/scripts/test_kanban_board_health.py

The two properties that matter, in this order:

  - a healthy board prints NOTHING. This script's whole value depends on being
    ignorable when the board is fine; the queries it carries are invariants
    rather than thresholds precisely so that this stays true.
  - each invariant actually fires on the shape it is meant to catch.

The live board these were validated against had zero rows outside
('done','archived'), so "returns nothing in production" proves nothing on its
own — the seeded fixtures below are where specificity is actually tested.
"""

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import kanban_board_health as kbh  # noqa: E402


# The columns the two queries touch, with the types the real board uses. Not
# the whole 37-column table on purpose: a fixture that mirrored the in-image
# schema would have to be re-copied every time upstream adds a column, and
# these tests are about the predicates, not about schema drift.
SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT NOT NULL,
    assignee TEXT,
    created_at INTEGER,
    claim_lock TEXT,
    current_run_id INTEGER,
    block_kind TEXT,
    result TEXT
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at INTEGER
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL
);
"""

STUCK_PAYLOAD = json.dumps([{
    "task_id": "t_stuck", "status": "blocked",
    "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                     "title": "Task has been blocked for 456h", "detail": "…",
                     "data": {"blocked_at": 1, "age_hours": 456.2}}],
}])


def make_board(tmp: Path, tasks=(), runs=(), events=(), links=()):
    db = tmp / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    for row in tasks:
        conn.execute(
            "INSERT INTO tasks (id,title,status,assignee,created_at,claim_lock,current_run_id,block_kind,result)"
            " VALUES (:id,:title,:status,:assignee,:created_at,:claim_lock,:current_run_id,:block_kind,:result)",
            {"title": "t", "assignee": "platform", "created_at": 1000,
             "claim_lock": None, "current_run_id": None, "block_kind": None, "result": None, **row},
        )
    for row in events:
        conn.execute(
            "INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
            (row["task_id"], row["kind"], json.dumps(row.get("payload") or {}), row.get("created_at", 1000)),
        )
    for row in runs:
        cur = conn.execute(
            "INSERT INTO task_runs (task_id,status,started_at) VALUES (?,?,?)",
            (row["task_id"], row["status"], row.get("started_at", 1000)),
        )
        if row.get("link_current"):
            conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?",
                         (cur.lastrowid, row["task_id"]))
    for parent_id, child_id in links:
        conn.execute(
            "INSERT INTO task_links (parent_id,child_id) VALUES (?,?)",
            (parent_id, child_id),
        )
    conn.commit()
    conn.close()
    return db


class InvariantTests(unittest.TestCase):
    def violations_for(self, **kwargs):
        with tempfile.TemporaryDirectory() as d:
            db = make_board(Path(d), **kwargs)
            conn = kbh.read_only_connection(db)
            try:
                return kbh.invariant_violations(conn)
            finally:
                conn.close()

    def test_healthy_board_is_silent(self):
        # A running card with a live claim and a matching run, plus finished
        # work: the ordinary steady state. Nothing may be reported.
        out = self.violations_for(
            tasks=[{"id": "t_run", "status": "running", "claim_lock": "host:7"},
                   {"id": "t_done", "status": "done"},
                   {"id": "t_ready", "status": "ready"},
                   {"id": "t_blocked", "status": "blocked"},
                   {"id": "t_triage", "status": "triage"}],
            runs=[{"task_id": "t_run", "status": "running", "link_current": True},
                  {"task_id": "t_done", "status": "done"}],
        )
        self.assertEqual(out, [])

    def test_empty_board_is_silent(self):
        self.assertEqual(self.violations_for(), [])

    def test_ghost_claim_on_non_running_card(self):
        out = self.violations_for(
            tasks=[{"id": "t_ghost", "status": "ready", "claim_lock": "host:99"}])
        self.assertEqual(len(out), 1)
        self.assertIn("ghost claim", out[0])
        self.assertIn("t_ghost", out[0])

    def test_done_open_pr_triage_card_without_linked_child_is_reported(self):
        out = self.violations_for(
            tasks=[{
                "id": "t_orphan_triage",
                "title": "Triage ns/Pod/api (BackOff) on c1",
                "status": "done",
                "assignee": "cluster-c1",
                "result": "- **To authorize:** a GitOps Pull Request for the recommended fix is being opened automatically.",
            }]
        )
        self.assertEqual(len(out), 1)
        self.assertIn("missing follow-up child", out[0])
        self.assertIn("t_orphan_triage", out[0])

        # With the follow-up child linked in task_links, the invariant is silent.
        linked = self.violations_for(
            tasks=[
                {
                    "id": "t_triage",
                    "title": "Triage ns/Pod/api (BackOff) on c1",
                    "status": "done",
                    "assignee": "cluster-c1",
                    "result": "- **To authorize:** a GitOps Pull Request for the recommended fix is being opened automatically.",
                },
                {"id": "t_pr", "title": "Open GitOps PR for ns/Pod/api (BackOff) on c1", "status": "todo", "assignee": "platform"},
            ],
            links=[("t_triage", "t_pr")],
        )
        self.assertEqual(linked, [])

    def test_orphan_run_when_task_is_not_running(self):
        out = self.violations_for(
            tasks=[{"id": "t_orph", "status": "done"}],
            runs=[{"task_id": "t_orph", "status": "running"}])
        self.assertEqual(len(out), 1)
        self.assertIn("orphan run", out[0])

    def test_orphan_run_when_task_points_at_a_different_run(self):
        # The shape a direct db write leaves: status still 'running', but the
        # card was re-pointed at a newer run and the old one never ended.
        out = self.violations_for(
            tasks=[{"id": "t_two", "status": "running", "claim_lock": "host:1"}],
            runs=[{"task_id": "t_two", "status": "running"},
                  {"task_id": "t_two", "status": "running", "link_current": True}])
        self.assertEqual(len(out), 1)
        self.assertIn("orphan run", out[0])

    def test_a_finished_run_on_a_running_card_is_not_an_orphan(self):
        # An earlier attempt that ended cleanly, plus the live one. Only runs
        # still claiming to be 'running' can be orphans.
        out = self.violations_for(
            tasks=[{"id": "t_retry", "status": "running", "claim_lock": "host:2"}],
            runs=[{"task_id": "t_retry", "status": "reclaimed"},
                  {"task_id": "t_retry", "status": "running", "link_current": True}])
        self.assertEqual(out, [])

    def test_connection_is_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            db = make_board(Path(d), tasks=[{"id": "t", "status": "done"}])
            conn = kbh.read_only_connection(db)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE tasks SET status='ready' WHERE id='t'")
            conn.close()

    def test_a_missing_board_is_not_created_by_opening_it(self):
        """`mode=ro` must refuse rather than conjure an empty board."""
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "kanban.db"
            with self.assertRaises(sqlite3.OperationalError):
                kbh.read_only_connection(missing)
            self.assertFalse(missing.exists())


class DiagnosticsTests(unittest.TestCase):
    def runner_for(self, code, out, err=""):
        captured = {}

        def runner(argv, env, timeout):
            captured["argv"] = argv
            captured["env"] = env
            return code, out, err

        return runner, captured

    def test_clean_engine_is_silent(self):
        runner, cap = self.runner_for(0, "[]\n")
        self.assertEqual(
            kbh.diagnostics_lines(Path("/opt/data"), "error", runner=runner,
                                  binary="/x/hermes", env={}), [])
        # Asked for everything; the floor is applied here so an always-report
        # kind reaches this script however low the engine grades it.
        self.assertEqual(cap["argv"][1:],
                         ["kanban", "diagnostics", "--json", "--severity", "warning"])
        self.assertEqual(cap["env"]["HERMES_HOME"], "/opt/data")
        self.assertEqual(cap["env"]["HERMES_KANBAN_DB"], "/opt/data/kanban.db")

    def test_a_warning_below_the_floor_is_filtered_here(self):
        payload = json.dumps([{
            "task_id": "t_1", "status": "ready",
            "diagnostics": [{"kind": "stranded_in_ready", "severity": "warning",
                             "title": "Stranded", "detail": "15m"}],
        }])
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={})
        self.assertEqual(lines, [])

    def test_stuck_in_blocked_is_reported_under_the_error_floor(self):
        context = {"t_stuck": {"title": "Triage x", "assignee": "cluster-a", "kind": "needs_input",
                               "reason": "judge rejected the report"}}
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, STUCK_PAYLOAD)[0],
                                      binary="/x/hermes", env={}, context=context)
        text = "\n".join(lines)
        self.assertIn("Cards blocked with no one looking", text)
        self.assertIn("t_stuck (cluster-a, needs_input, blocked 19d): Triage x", text)
        self.assertIn('"judge rejected the report"', text)
        self.assertIn("hermes kanban unblock --reason", text)
        self.assertIn("hermes kanban archive t_stuck", text)

    def test_stuck_card_names_todo_children_held_behind_it(self):
        with tempfile.TemporaryDirectory() as d:
            db = make_board(
                Path(d),
                tasks=[
                    {"id": "t_stuck", "title": "Triage x", "status": "blocked", "assignee": "cluster-a", "block_kind": "needs_input"},
                    {"id": "t_pr", "title": "Open GitOps PR for x", "status": "todo", "assignee": "platform"},
                ],
                links=[("t_stuck", "t_pr")],
            )
            conn = kbh.read_only_connection(db)
            try:
                context = kbh.blocked_context(conn)
            finally:
                conn.close()
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, STUCK_PAYLOAD)[0],
                                      binary="/x/hermes", env={}, context=context)
        text = "\n".join(lines)
        self.assertIn("holding todo child: t_pr (platform): Open GitOps PR for x", text)

    def test_a_stuck_card_without_context_falls_back_to_the_cli_s_fields(self):
        # An unmigrated board (no block_kind, no task_events) yields no context;
        # the CLI's own JSON still carries the title and assignee.
        payload = json.dumps([{
            "task_id": "t_stuck", "status": "blocked", "title": "From the CLI", "assignee": "platform",
            "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning", "title": "x",
                             "detail": "y", "data": {"age_hours": 456.2}}],
        }])
        lines = kbh.diagnostics_lines(Path("/data/moved"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context={})
        text = "\n".join(lines)
        self.assertIn("t_stuck (platform, no kind, blocked 19d): From the CLI", text)
        # The operator commands name the home this run actually read.
        self.assertIn("HERMES_HOME=/data/moved HERMES_KANBAN_DB=/data/moved/kanban.db hermes kanban archive t_stuck", text)

    def test_always_report_can_be_narrowed_by_env(self):
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, STUCK_PAYLOAD)[0],
                                      binary="/x/hermes", env={}, context={}, always=frozenset())
        self.assertEqual(lines, [])
        self.assertEqual(kbh.always_report_kinds({"KANBAN_HEALTH_ALWAYS_REPORT": "a, b"}), frozenset({"a", "b"}))
        self.assertEqual(kbh.always_report_kinds({}), kbh.ALWAYS_REPORT_KINDS)

    def test_age_renders_hours_below_two_days_and_days_after(self):
        self.assertEqual(kbh.render_age(3.9), "3h")
        self.assertEqual(kbh.render_age(47.9), "47h")
        self.assertEqual(kbh.render_age(48), "2d")
        self.assertEqual(kbh.render_age(None), "?")

    def test_findings_are_rendered(self):
        payload = json.dumps([{
            "task_id": "t_1", "status": "blocked",
            "diagnostics": [{"kind": "repeated_failures", "severity": "error",
                             "title": "Repeated failures", "detail": "2 in a row"}],
        }])
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={})
        self.assertEqual(len(lines), 1)
        self.assertIn("[error] t_1 (blocked)", lines[0])
        self.assertIn("Repeated failures", lines[0])

    def test_a_multi_paragraph_detail_is_cut_to_its_first_line(self):
        payload = json.dumps([{
            "task_id": "t_1", "status": "blocked",
            "diagnostics": [{"kind": "repeated_failures", "severity": "error",
                             "title": "Agent crash x3", "detail": "first line\n\nsecond paragraph\nthird"}],
        }])
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={})
        self.assertEqual(lines, ["  [error] t_1 (blocked): Agent crash x3 - first line"])

    def test_log_prefix_before_json_is_tolerated(self):
        runner, _ = self.runner_for(0, "WARNING: something\n[]\n")
        self.assertEqual(
            kbh.diagnostics_lines(Path("/opt/data"), "error", runner=runner,
                                  binary="/x/hermes", env={}), [])

    def test_cli_failure_is_reported_not_swallowed(self):
        runner, _ = self.runner_for(2, "", "boom: no such board\n")
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error", runner=runner,
                                      binary="/x/hermes", env={})
        self.assertEqual(len(lines), 1)
        self.assertIn("diagnostics unavailable", lines[0])
        self.assertIn("boom", lines[0])

    def test_runner_exception_is_reported(self):
        def runner(argv, env, timeout):
            raise TimeoutError("took too long")
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error", runner=runner,
                                      binary="/x/hermes", env={})
        self.assertIn("diagnostics unavailable", lines[0])

    def test_unparseable_output_is_reported(self):
        runner, _ = self.runner_for(0, "not json at all")
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error", runner=runner,
                                      binary="/x/hermes", env={})
        self.assertEqual(len(lines), 1)
        self.assertIn("diagnostics unavailable", lines[0])

    def test_severity_floor_defaults_and_validates(self):
        self.assertEqual(kbh.severity_floor(env={}), "error")
        self.assertEqual(kbh.severity_floor(env={"KANBAN_HEALTH_SEVERITY": "warning"}),
                         "warning")
        self.assertEqual(kbh.severity_floor(env={"KANBAN_HEALTH_SEVERITY": "nonsense"}),
                         "error")

    def test_commands_appear_once_in_footer_with_multiple_cards(self):
        payload = json.dumps([
            {"task_id": "t_1", "status": "blocked", "title": "Card 1",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 10}}]},
            {"task_id": "t_2", "status": "blocked", "title": "Card 2",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 20}}]},
        ])
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context={})
        text = "\n".join(lines)
        self.assertIn("Commands to move a card (run on the agent pod):", text)
        self.assertIn("<task_id>", text)
        self.assertEqual(text.count("re-run it:"), 1)
        self.assertEqual(text.count("drop it:"), 1)

    def test_stuck_cards_sorted_newest_first(self):
        payload = json.dumps([
            {"task_id": "t_old", "status": "blocked", "title": "Old card",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 216}}]},
            {"task_id": "t_new", "status": "blocked", "title": "New card",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 2}}]},
            {"task_id": "t_mid", "status": "blocked", "title": "Mid card",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 48}}]},
        ])
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context={})
        text = "\n".join(lines)
        pos_new = text.find("t_new")
        pos_mid = text.find("t_mid")
        pos_old = text.find("t_old")
        self.assertTrue(0 <= pos_new < pos_mid < pos_old,
                        f"Expected newest first order: pos_new={pos_new}, pos_mid={pos_mid}, pos_old={pos_old}")

    def test_cards_sharing_title_are_grouped(self):
        payload = json.dumps([
            {"task_id": "t_1", "status": "blocked", "title": "Scale kube-dns on platform-agent-host",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 216}}]},
            {"task_id": "t_2", "status": "blocked", "title": "Scale kube-dns on platform-agent-host",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 120}}]},
            {"task_id": "t_3", "status": "blocked", "title": "Scale kube-dns on platform-agent-host",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 120}}]},
            {"task_id": "t_4", "status": "blocked", "title": "Scale kube-dns on platform-agent-host",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 48}}]},
        ])
        context = {
            "t_1": {"kind": "capability"},
            "t_2": {"kind": "needs_input"},
            "t_3": {"kind": "needs_input"},
            "t_4": {"kind": "transient"},
        }
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context=context)
        text = "\n".join(lines)
        self.assertIn("Scale kube-dns on platform-agent-host — 4 cards (2 needs_input, 1 capability, 1 transient, oldest 9d)", text)
        self.assertEqual(text.count("Scale kube-dns on platform-agent-host"), 1)
        self.assertIn("latest: [t_4] (transient, 2d)", text)

    def test_thirty_four_cards_scenario_fits_within_chat_limit(self):
        # Reproduce Hang's 34-card bug bash scenario (#3470)
        # 24 cards on "Scale kube-dns on platform-agent-host to 3 replicas" / near-copies
        # 6 triage cards for an outage from 2d ago
        # 4 miscellaneous cards
        cards = []
        context = {}
        for i in range(24):
            tid = f"t_dns_{i}"
            kind = "needs_input" if i < 18 else ("transient" if i < 22 else "capability")
            age = 216 if i < 2 else (120 if i < 23 else 96)
            cards.append({
                "task_id": tid, "status": "blocked",
                "title": "Scale kube-dns on platform-agent-host to 3 replicas",
                "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                                 "title": "x", "detail": "y", "data": {"age_hours": age}}],
            })
            context[tid] = {
                "kind": kind,
                "reason": "Direct mutation of GKE resources is blocked by command policy: 'Agents hold read-only access to Kubernetes. Propose this change as a pull request instead...'",
            }

        # 6 outage triage cards from 2 days ago (48h)
        for i in range(6):
            tid = f"t_triage_{i}"
            pod = f"platform-agent-gateway-pod-{i}"
            cards.append({
                "task_id": tid, "status": "blocked",
                "title": f"Triage {pod} (Failed) on platform-agent-host",
                "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                                 "title": "x", "detail": "y", "data": {"age_hours": 48}}],
            })
            context[tid] = {
                "kind": "needs_input",
                "reason": "The pinned kubeconfig is for a different cluster than this agent is scoped to.",
            }

        # 4 miscellaneous cards
        for i in range(4):
            tid = f"t_misc_{i}"
            cards.append({
                "task_id": tid, "status": "blocked",
                "title": f"Miscellaneous task {i}",
                "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                                 "title": "x", "detail": "y", "data": {"age_hours": 72}}],
            })
            context[tid] = {
                "kind": "transient",
                "reason": f"Transient error {i}",
            }

        payload = json.dumps(cards)
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context=context)
        text = "\n".join(lines)

        # 1. Output length must be well below 12,000 characters
        self.assertLess(len(text), 3000, f"Expected compact report under 3000 chars, got {len(text)}")

        # 2. The 24 kube-dns cards collapsed into one group line
        self.assertIn("Scale kube-dns on platform-agent-host to 3 replicas — 24 cards (18 needs_input, 4 transient, 2 capability, oldest 9d)", text)

        # 3. The 6 recent outage triage cards (48h) appear BEFORE the kube-dns cards whose newest is 96h (4d)
        pos_triage = text.find("t_triage_0")
        pos_dns_group = text.find("Scale kube-dns on platform-agent-host to 3 replicas — 24 cards")
        self.assertTrue(0 <= pos_triage < pos_dns_group,
                        f"Expected outage triage cards (2d) before kube-dns group (4d): pos_triage={pos_triage}, pos_dns_group={pos_dns_group}")

        # 4. Commands footer appears once with <task_id> placeholder
        self.assertEqual(text.count("re-run it:"), 1)
        self.assertEqual(text.count("drop it:"), 1)
        self.assertIn("<task_id>", text)

    def test_small_group_renders_each_card_with_task_id_and_reason(self):
        payload = json.dumps([
            {"task_id": "t_scale_1", "status": "blocked", "title": "Scale kube-dns",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 24}}]},
            {"task_id": "t_scale_2", "status": "blocked", "title": "Scale kube-dns",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 48}}]},
        ])
        context = {
            "t_scale_1": {"kind": "needs_input", "reason": "Blocked on human approval"},
            "t_scale_2": {"kind": "capability", "reason": "Missing gcloud tool"},
        }
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context=context)
        text = "\n".join(lines)
        self.assertIn("Scale kube-dns — 2 cards (1 capability, 1 needs_input, oldest 2d)", text)
        self.assertIn('[t_scale_1] (needs_input, 24h) — "Blocked on human approval"', text)
        self.assertIn('[t_scale_2] (capability, 2d) — "Missing gcloud tool"', text)

    def test_large_group_renders_latest_card_and_ids(self):
        payload = json.dumps([
            {"task_id": f"t_dns_{i}", "status": "blocked", "title": "Scale kube-dns",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 24 * (i + 1)}}]}
            for i in range(12)
        ])
        context = {
            f"t_dns_{i}": {"kind": "needs_input", "reason": f"Reason {i}"}
            for i in range(12)
        }
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context=context)
        text = "\n".join(lines)
        self.assertIn("Scale kube-dns — 12 cards (12 needs_input, oldest 12d)", text)
        self.assertIn('latest: [t_dns_0] (needs_input, 24h) — "Reason 0"', text)
        expected_ids = ", ".join(f"t_dns_{i}" for i in range(12))
        self.assertIn(f"ids: {expected_ids}", text)
        self.assertNotIn("and 2 more", text)

    def test_distinct_title_outage_report_bounded_with_omitted_summary(self):
        # 30 cards with distinct titles (wide outage scenario)
        cards = [
            {"task_id": f"t_outage_{i}", "status": "blocked",
             "title": f"Triage stalled controller {i} in prod on cluster-1: pod-{i}",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 24 + i}}]}
            for i in range(30)
        ]
        context = {
            f"t_outage_{i}": {
                "kind": "needs_input",
                "reason": f"Controller {i} is crashlooping with OOMKilled status on pod-{i}." * 3,
            }
            for i in range(30)
        }
        payload = json.dumps(cards)
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context=context)
        text = "\n".join(lines)
        # Bounded well below 12,000 chars limit
        self.assertLess(len(text), 12000)
        # Omission message indicates older cards omitted with task IDs
        self.assertIn("older stuck cards omitted (ids: ", text)
        self.assertIn("run hermes kanban list on the agent pod to view all", text)
        # Commands footer is preserved at the end
        self.assertIn("Commands to move a card (run on the agent pod):", text)
        self.assertIn("re-run it:", text)
        self.assertIn("drop it:", text)
        # Newest cards appear in full detail, oldest omitted from detailed list but named in omitted ids
        self.assertIn("Triage stalled controller 0 in prod", text)
        self.assertNotIn("Triage stalled controller 29 in prod", text)
        self.assertIn("t_outage_29", text)

    def test_stuck_cards_bounded_by_max_chars_when_items_have_long_reasons(self):
        # 8 groups of 3 cards each = 8 renderable items (< MAX_STUCK_ITEMS which is 15).
        # Each card has a max-length reason (~200 chars), so each 3-card group renders
        # header + 3 indented lines totaling ~840 characters.
        # 8 items * ~840 chars = ~6720 chars, which exceeds MAX_STUCK_CHARS (5500).
        # The character cap MUST trip before the item cap (breaking at around item 6 < 15).
        cards = []
        context = {}
        for g in range(8):
            title = f"Database replica lag incident {g}"
            for c in range(3):
                tid = f"t_grp_{g}_{c}"
                cards.append({
                    "task_id": tid,
                    "status": "blocked",
                    "title": title,
                    "diagnostics": [{
                        "kind": "stuck_in_blocked",
                        "severity": "warning",
                        "title": "x",
                        "detail": "y",
                        "data": {"age_hours": 24 * (g + 1) + c},
                    }],
                })
                context[tid] = {
                    "kind": "needs_input",
                    "reason": f"Postgres read replica on shard {g} node {c} exceeds replication lag threshold by 500ms." * 2,
                }
        payload = json.dumps(cards)
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context=context)
        text = "\n".join(lines)
        # Verify fewer than MAX_STUCK_ITEMS (15) were rendered because character cap tripped
        self.assertIn("Database replica lag incident 0", text)
        # Later groups past the 5500 char cap must be omitted from full group rendering
        self.assertNotIn("[t_grp_7_0]", text)
        self.assertIn("older stuck cards omitted (ids: ", text)
        self.assertIn("t_grp_7_0", text)
        # Total text is strictly bounded under MAX_STUCK_CHARS + headers/footers
        self.assertLess(len(text), 7000)

    def test_non_stuck_diagnostics_and_stuck_cards_bounded_together(self):
        # 30 non-stuck error diagnostics + 20 stuck cards
        cards = []
        for i in range(30):
            cards.append({
                "task_id": f"t_err_{i}",
                "status": "failed",
                "diagnostics": [{
                    "kind": "repeated_failures",
                    "severity": "error",
                    "title": f"Worker crashed on iteration {i}",
                    "detail": f"Pod OOMKilled in namespace kubeagents-system with exit code 137 on replica {i}." * 2,
                }],
            })
        for i in range(20):
            cards.append({
                "task_id": f"t_stuck_{i}",
                "status": "blocked",
                "title": f"Triage deadlock {i}",
                "diagnostics": [{
                    "kind": "stuck_in_blocked",
                    "severity": "warning",
                    "title": "x",
                    "detail": "y",
                    "data": {"age_hours": 24 + i},
                }],
            })
        payload = json.dumps(cards)
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context={})
        text = "\n".join(lines)
        # Non-stuck diagnostics are bounded
        self.assertIn("other diagnostics omitted (run hermes kanban diagnostics on the agent pod to view all)", text)
        # Stuck cards are also bounded
        self.assertIn("older stuck cards omitted (ids: ", text)
        # Commands footer remains intact
        self.assertIn("Commands to move a card (run on the agent pod):", text)
        # Whole report strictly respects budget well below 12,000 chars
        self.assertLess(len(text), kbh.MAX_REPORT_CHARS)

    def test_non_stuck_diagnostics_bounded_by_max_items_when_lines_are_short(self):
        # 25 non-stuck error diagnostics of ~80 chars each.
        # Total chars for 25 lines = ~2000 chars, well below MAX_DIAGNOSTIC_CHARS (3500).
        # Therefore the item cap (MAX_DIAGNOSTIC_ITEMS = 20) MUST trip before the character cap,
        # asserting exactly 20 items are shown and 5 are omitted.
        cards = []
        for i in range(25):
            cards.append({
                "task_id": f"t_item_{i:02d}",
                "status": "failed",
                "diagnostics": [{
                    "kind": "repeated_failures",
                    "severity": "error",
                    "title": f"Err {i:02d}",
                    "detail": "short detail",
                }],
            })
        payload = json.dumps(cards)
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={})
        text = "\n".join(lines)
        self.assertIn("t_item_00", text)
        self.assertIn("t_item_19", text)
        self.assertNotIn("t_item_20", text)
        self.assertIn("... and 5 other diagnostics omitted", text)

    def test_stuck_budget_derived_from_remaining_report_allowance(self):
        # Non-stuck diagnostics sit near cap (3500 chars).
        # Stuck cards have 10 items of ~300 chars each = ~3000 chars.
        # When max_chars is 5000, remaining budget for stuck cards is ~2000 chars.
        # Stuck cards must be bounded by the derived budget, omitting later cards.
        # With bare MAX_STUCK_CHARS (5500), all 3000 chars would fit without omission.
        cards = []
        for i in range(16):
            cards.append({
                "task_id": f"t_diag_{i:02d}",
                "status": "failed",
                "diagnostics": [{
                    "kind": "repeated_failures",
                    "severity": "error",
                    "title": f"Failure {i:02d}",
                    "detail": "x" * 150,
                }],
            })
        context = {}
        for i in range(10):
            tid = f"t_stuck_{i:02d}"
            cards.append({
                "task_id": tid,
                "status": "blocked",
                "title": f"Stuck item {i:02d}",
                "diagnostics": [{
                    "kind": "stuck_in_blocked",
                    "severity": "warning",
                    "title": "x",
                    "detail": "y",
                    "data": {"age_hours": 24 + i},
                }],
            })
            context[tid] = {
                "kind": "needs_input",
                "reason": "z" * 180,
            }
        payload = json.dumps(cards)
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={}, context=context,
                                      max_chars=5000)
        text = "\n".join(lines)
        # Diagnostics at cap
        self.assertIn("t_diag_00", text)
        # Derived budget cut stuck cards around 2000 chars
        self.assertIn("t_stuck_00", text)
        self.assertNotIn("Stuck item 09", text)
        self.assertIn("older stuck cards omitted", text)
        self.assertIn("t_stuck_06", text)
        # Preserves commands footer
        self.assertIn("Commands to move a card", text)
        self.assertLessEqual(len(text), 5000)

    def test_hundreds_of_omitted_cards_bounded_without_overflow(self):
        # 20 error diagnostics at cap (~3500 chars) + 500 stuck cards (~5820 chars omitted).
        # Total without omission bounding would be ~10,600 chars, blowing past MAX_REPORT_CHARS (8500).
        # Verifies omission IDs are truncated with ', and N more', oldest card t_big_0499 is not named,
        # and total report length strictly stays under MAX_REPORT_CHARS while preserving footer.
        cards = []
        for i in range(20):
            cards.append({
                "task_id": f"t_err_{i:02d}",
                "status": "failed",
                "diagnostics": [{
                    "kind": "repeated_failures",
                    "severity": "error",
                    "title": f"Err {i:02d}",
                    "detail": "x" * 150,
                }],
            })
        for i in range(500):
            cards.append({
                "task_id": f"t_big_{i:04d}",
                "status": "blocked",
                "title": f"Outage incident {i:04d}",
                "diagnostics": [{
                    "kind": "stuck_in_blocked",
                    "severity": "warning",
                    "title": "x",
                    "detail": "y",
                    "data": {"age_hours": 24 + i},
                }],
            })
        payload = json.dumps(cards)
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={})
        text = "\n".join(lines)
        self.assertLess(len(text), kbh.MAX_REPORT_CHARS)
        self.assertIn("older stuck cards omitted", text)
        self.assertIn(", and ", text)
        self.assertNotIn("t_big_0499", text)
        self.assertIn("Commands to move a card (run on the agent pod):", text)
        self.assertIn("re-run it:", text)
        self.assertIn("drop it:", text)

    def test_single_large_group_with_hundreds_of_cards_bounded(self):
        # A large group with 500 cards sharing the same title
        cards = [
            {"task_id": f"t_dup_{i:04d}", "status": "blocked", "title": "Scaling issue",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 24 + i}}]}
            for i in range(500)
        ]
        payload = json.dumps(cards)
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={})
        text = "\n".join(lines)
        self.assertLess(len(text), kbh.MAX_REPORT_CHARS)
        self.assertIn("Scaling issue — 500 cards", text)
        self.assertIn("ids:", text)
        self.assertIn("more", text)
        self.assertIn("older stuck cards omitted", text)
        # Dropped IDs from group are included in the omission summary up to budget
        self.assertIn("older stuck cards omitted (ids: ", text)
        self.assertIn("t_dup_0083", text)
        self.assertIn("Commands to move a card (run on the agent pod):", text)

    def test_group_with_sixty_cards_lists_all_ids_within_budget(self):
        # 60 cards sharing one title (the #3470 retry-loop shape).
        # IDs take 60 * 12 = 720 chars, which comfortably fits in available_for_items (~5000 chars).
        # All 60 IDs must be listed directly without truncation or omission.
        cards = [
            {"task_id": f"t_dup_{i:04d}", "status": "blocked", "title": "Scaling issue",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 24 + i}}]}
            for i in range(60)
        ]
        payload = json.dumps(cards)
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={})
        text = "\n".join(lines)
        self.assertLess(len(text), kbh.MAX_REPORT_CHARS)
        self.assertIn("Scaling issue — 60 cards", text)
        self.assertIn("t_dup_0000", text)
        self.assertIn("t_dup_0059", text)
        self.assertNotIn("more", text)
        self.assertNotIn("older stuck cards omitted", text)

    def test_non_string_task_id_and_null_status_do_not_raise(self):
        # Coerce malformed CLI output (task_id: null, task_id: int, status: null)
        # without raising TypeError in sort, join, or formatting when multiple cards
        # share a title or tie on age_hours.
        cards = [
            {
                "task_id": None,
                "status": None,
                "title": "Shared task title",
                "diagnostics": [{
                    "kind": "stuck_in_blocked",
                    "severity": "warning",
                    "data": {"age_hours": 10},
                }],
            },
            {
                "task_id": 12345,
                "status": "blocked",
                "title": "Shared task title",
                "diagnostics": [{
                    "kind": "stuck_in_blocked",
                    "severity": "warning",
                    "data": {"age_hours": 10},
                }],
            },
            {
                "task_id": None,
                "status": "blocked",
                "title": "Shared task title",
                "diagnostics": [{
                    "kind": "stuck_in_blocked",
                    "severity": "warning",
                    "data": {"age_hours": 10},
                }],
            },
            {
                "task_id": 67890,
                "status": "blocked",
                "title": "Shared task title",
                "diagnostics": [{
                    "kind": "stuck_in_blocked",
                    "severity": "warning",
                    "data": {"age_hours": 10},
                }],
            },
            {
                "task_id": None,
                "status": "failed",
                "diagnostics": [{
                    "kind": "repeated_failures",
                    "severity": "error",
                    "title": "Failure without id",
                    "detail": "some detail",
                }],
            },
        ]
        payload = json.dumps(cards)
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={})
        text = "\n".join(lines)
        self.assertIn("12345", text)
        self.assertIn("?", text)
        self.assertIn("Failure without id", text)

    def test_last_item_fits_in_omission_reserve_promoted_without_omission_line(self):
        # Two distinct-title cards.
        # Header = 113 chars, footer with Path("/opt/data") = 281 chars.
        # max_chars = 614 -> budget_for_body = 220, available_for_items = 100.
        # Card 1 (~56 chars) fits available_for_items.
        # Card 2 (~56 chars) does not fit available_for_items (112 > 100).
        # But Card 2 fits budget_for_body (112 <= 220) and no items have dropped_ids,
        # so it is promoted by the last-item exemption without an omission line.
        cards = [
            ("t_01", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10}},
             {"task_id": "t_01", "status": "blocked", "title": "First card"}),
            ("t_02", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 12}},
             {"task_id": "t_02", "status": "blocked", "title": "Second card"}),
        ]
        lines = kbh.render_stuck_cards(cards, {}, Path("/opt/data"), max_chars=580)
        text = "\n".join(lines)
        self.assertIn("t_01", text)
        self.assertIn("t_02", text)
        self.assertNotIn("omitted", text)
        self.assertLessEqual(len(text), 580)

    def test_last_item_large_group_promoted_when_fits_in_omission_reserve_with_no_dropped_ids(self):
        # A single card fits in available_for_items. A 4-card large group's head exceeds
        # the remaining available_for_items (triggering continue at L582-583), leaving it
        # as the single untaken item. In last-item promotion, the large group fits in the
        # omission reserve (budget_for_body - current_chars) without dropping any IDs (L602-606).
        # Header (113) + Footer (281) = 394. max_chars=634 -> budget_for_body=240, available_for_items=120.
        # S card (~42 chars) leaves 78 in available_for_items.
        # Group head (~86 chars) > 78 so skipped in taking loop.
        # Promotion budget = 240 - 42 = 198 chars. All 4 IDs fit in group_ids_budget.
        cards = [
            ("t_new", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 1}},
             {"task_id": "t_new", "status": "blocked", "title": "Fresh"}),
        ] + [
            (f"t_g_{i}", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 2 + i}},
             {"task_id": f"t_g_{i}", "status": "blocked", "title": "Repeated title"})
            for i in range(4)
        ]
        lines = kbh.render_stuck_cards(cards, {}, Path("/opt/data"), max_chars=634)
        text = "\n".join(lines)
        self.assertIn("ids: t_g_0, t_g_1, t_g_2, t_g_3", text)
        self.assertIn("Fresh", text)
        self.assertNotIn("omitted", text)
        self.assertLessEqual(len(text), 634)

    def test_last_item_large_group_declined_when_ids_exceed_omission_reserve(self):
        # Sibling fixture: same single card + 20-card group.
        # The 20-card group cannot fit all IDs in the promotion budget without dropping IDs.
        # Because promo_item has dropped_ids, L604 declines promotion and preserves room
        # for the omission summary line.
        cards = [
            ("t_new", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 1}},
             {"task_id": "t_new", "status": "blocked", "title": "Fresh"}),
        ] + [
            (f"t_g_{i:02d}", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 2 + i}},
             {"task_id": f"t_g_{i:02d}", "status": "blocked", "title": "Repeated title"})
            for i in range(20)
        ]
        lines = kbh.render_stuck_cards(cards, {}, Path("/opt/data"), max_chars=634)
        text = "\n".join(lines)
        self.assertIn("Fresh", text)
        self.assertIn("omitted", text)
        self.assertNotIn("Repeated title", text)
        self.assertNotIn("    ids:", text)
        self.assertLessEqual(len(text), 634)

    def test_last_item_not_promoted_when_earlier_group_has_dropped_ids(self):
        # When an earlier group folded IDs (has dropped_ids), the last item must NOT be promoted
        # even if it fits in budget_for_body, ensuring the omission reserve is preserved.
        # 10 cards sharing a title -> large group.
        # 1 distinct card as last item.
        cards = [
            (f"t_grp_{i:02d}", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10 + i}},
             {"task_id": f"t_grp_{i:02d}", "status": "blocked", "title": "Repeated title"})
            for i in range(10)
        ]
        cards.append(
            ("t_last", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 50}},
             {"task_id": "t_last", "status": "blocked", "title": "Last card"})
        )
        # Header (113) + Footer (281) = 394.
        # We want available_for_items to fit the group (with dropped IDs) but not the last item.
        # Group: head ~60 chars + latest ~55 chars + ids ~50 chars = ~165 chars.
        # Last card line: ~53 chars.
        # Let available_for_items = 180, budget_for_body = 300 -> max_chars = 694.
        # Group takes ~165 <= 180. Group folds ~5 IDs into dropped_ids.
        # Group + Last card = 165 + 53 = 218 <= 300 (budget_for_body).
        # Without checking dropped_ids across taken items, Last card would be promoted,
        # spending the omission reserve and causing overflow when omitted_cards > 0 emits summary.
        # With the fix, Last card is not promoted, leaving room for omission summary.
        lines = kbh.render_stuck_cards(cards, {}, Path("/opt/data"), max_chars=694)
        text = "\n".join(lines)
        self.assertIn("Repeated title", text)
        self.assertIn("omitted", text)
        self.assertNotIn("t_last", text)
        self.assertLessEqual(len(text), 694)

    def test_group_ids_line_omitted_when_budget_cannot_fit_single_id(self):
        # When group_ids_budget is too small to fit even one ID, 'ids: and N more' is not rendered,
        # and all IDs are carried into dropped_ids and omission summary.
        cards = [
            (f"t_grp_{i:02d}", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10 + i}},
             {"task_id": f"t_grp_{i:02d}", "status": "blocked", "title": "Repeated title"})
            for i in range(5)
        ]
        # Set max_chars so available_for_items fits head and latest_sub, but group_ids_budget is 0.
        # Header 113 + Footer 281 = 394.
        # Group head (60) + latest_sub (55) = 115 chars.
        # With group_header_chars = 115 + 1 + len("    ids: ") + 1 = 126.
        # If available_for_items = 120, group_ids_budget = max(0, 120 - 126) = 0.
        # max_chars = 394 + 120 + 120 = 634.
        lines = kbh.render_stuck_cards(cards, {}, Path("/opt/data"), max_chars=634)
        text = "\n".join(lines)
        self.assertIn("Repeated title", text)
        self.assertNotIn("ids: and", text)
        self.assertIn("latest: [t_grp_00]", text)
        self.assertIn("... and 4 older stuck cards omitted", text)
        self.assertNotIn("t_grp_00", text.split("omitted", 1)[1])
        self.assertLessEqual(len(text), 634)

    def test_oversized_newest_item_skipped_allowing_subsequent_items_to_render(self):
        # When an oversized newest item exceeds available_for_items, _take_within_budget
        # skips it rather than breaking and emptying the section behind it.
        cards = [
            ("t_big_1", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10}},
             {"task_id": "t_big_1", "status": "blocked", "title": "Big Group"}),
            ("t_big_2", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10}},
             {"task_id": "t_big_2", "status": "blocked", "title": "Big Group"}),
            ("t_big_3", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10}},
             {"task_id": "t_big_3", "status": "blocked", "title": "Big Group"}),
            ("t_small_1", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 20}},
             {"task_id": "t_small_1", "status": "blocked", "title": "Small Card"}),
        ]
        context = {
            "t_big_1": {"reason": "x" * 200},
            "t_big_2": {"reason": "x" * 200},
            "t_big_3": {"reason": "x" * 200},
            "t_small_1": {"reason": "small"},
        }
        lines = kbh.render_stuck_cards(cards, context, Path("/opt/data"), max_chars=714)
        text = "\n".join(lines)
        self.assertIn("Small Card", text)
        self.assertIn("t_small_1", text)
        self.assertIn("... and 3 other stuck cards omitted", text)

    def test_promoted_item_rendered_newest_first_after_skipping(self):
        # When an oversized newest item is skipped during budget allocation but
        # later promoted into budget_for_body, taken_items is re-sorted so the
        # promoted newest item renders first rather than last.
        cards = [
            ("t_a", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 5}},
             {"task_id": "t_a", "status": "blocked", "title": "Card A"}),
            ("t_b", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 15}},
             {"task_id": "t_b", "status": "blocked", "title": "Card B"}),
            ("t_c", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 25}},
             {"task_id": "t_c", "status": "blocked", "title": "Card C"}),
        ]
        context = {
            "t_a": {"reason": "a" * 200},
        }
        lines = kbh.render_stuck_cards(cards, context, Path("/opt/data"), max_chars=744)
        text = "\n".join(lines)
        pos_a = text.find("Card A")
        pos_b = text.find("Card B")
        pos_c = text.find("Card C")
        self.assertNotEqual(pos_a, -1)
        self.assertNotEqual(pos_b, -1)
        self.assertNotEqual(pos_c, -1)
        self.assertLess(pos_a, pos_b, "Promoted newest card A must render before card B")
        self.assertLess(pos_b, pos_c, "Card B must render before card C")

    def test_omitted_ids_ordered_newest_first_when_omission_budget_constrained(self):
        # When a taken repeated group has dropped_ids (old copies) and an untaken item
        # has a newer card, omitted_ids must order the newer card first so the operator
        # sees the recent actionable ID rather than stale group copies when max_ids_budget truncates.
        cards = [
            (f"t_grp_{i}", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 30 + 10 * i}},
             {"task_id": f"t_grp_{i}", "status": "blocked", "title": "Group Title"})
            for i in range(6)
        ]
        cards.append(
            ("t_newer", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 15}},
             {"task_id": "t_newer", "status": "blocked", "title": "Distinct Untaken Card"})
        )
        context = {
            "t_newer": {"reason": "r" * 150},
        }
        lines = kbh.render_stuck_cards(cards, context, Path("/opt/data"), max_chars=625)
        text = "\n".join(lines)
        self.assertIn("ids: t_newer", text)
        self.assertIn("... and 6 other stuck cards omitted", text)

    def test_null_task_ids_in_group_not_suppressed_when_ids_budget_starved(self):
        # When cards with null task_id (coerced to '?') share a title and group_ids_budget
        # cannot fit IDs, position-based dropped_ids filtering removes only the shown card
        # so remaining cards are correctly counted and emitted in the omission summary.
        cards = [
            (None, {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10 + i}},
             {"task_id": None, "status": "blocked", "title": "Shared Title"})
            for i in range(5)
        ]
        lines = kbh.render_stuck_cards(cards, {}, Path("/opt/data"), max_chars=600)
        text = "\n".join(lines)
        self.assertIn("Shared Title", text)
        self.assertIn("... and 4 older stuck cards omitted", text)

    def test_task_id_prefixed_with_and_not_misidentified_as_formatter_sentinel(self):
        # A task ID starting with 'and ' (e.g. 'and 01') should not trigger an in-band
        # sentinel check and cause group IDs or omission lines to be suppressed.
        cards = [
            (f"and {i:02d}", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10 + i}},
             {"task_id": f"and {i:02d}", "status": "blocked", "title": "Repeated title"})
            for i in range(5)
        ]
        lines = kbh.render_stuck_cards(cards, {}, Path("/opt/data"), max_chars=2000)
        text = "\n".join(lines)
        self.assertIn("ids: and 00, and 01", text)

        cards_omitted = [
            (f"and {i:02d}", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10 + i}},
             {"task_id": f"and {i:02d}", "status": "blocked", "title": f"Distinct title {i}"})
            for i in range(4)
        ]
        lines2 = kbh.render_stuck_cards(cards_omitted, {}, Path("/opt/data"), max_chars=644)
        text2 = "\n".join(lines2)
        self.assertIn("older stuck cards omitted (ids: and ", text2)

    def test_oversized_title_clipped_to_title_max_chars(self):
        card = ("t_huge", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 24}},
                {"task_id": "t_huge", "status": "blocked", "title": "T" * 1000})
        lines = kbh.render_stuck_cards([card], {}, Path("/opt/data"))
        text = "\n".join(lines)
        self.assertIn("T" * kbh.TITLE_MAX_CHARS, text)
        self.assertNotIn("T" * (kbh.TITLE_MAX_CHARS + 1), text)

    def test_oversized_title_in_repeated_title_group_clipped_to_title_max_chars(self):
        cards = [
            ("t_huge_1", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 24}},
             {"task_id": "t_huge_1", "status": "blocked", "title": "T" * 1000}),
            ("t_huge_2", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 25}},
             {"task_id": "t_huge_2", "status": "blocked", "title": "T" * 1000}),
        ]
        lines = kbh.render_stuck_cards(cards, {}, Path("/opt/data"))
        text = "\n".join(lines)
        # Head line carries exactly TITLE_MAX_CHARS 'T's and no more
        self.assertIn(f"  - {'T' * kbh.TITLE_MAX_CHARS} — 2 cards", text)
        self.assertNotIn("T" * (kbh.TITLE_MAX_CHARS + 1), text)

    def test_min_stuck_floor_derived_from_actual_home_path(self):
        cards = [(f"t_stuck_{i:02d}", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 24 + i}},
                  {"task_id": f"t_stuck_{i:02d}", "status": "blocked", "title": f"Stuck {i:02d}"})
                 for i in range(10)]
        floor_short = kbh._min_stuck_floor(cards, Path("/opt/data"))
        floor_long = kbh._min_stuck_floor(cards, Path("/var/run/kube-agents/long/home/path"))
        # Floor scales with 4 * len(home)
        diff_len = len("/var/run/kube-agents/long/home/path") - len("/opt/data")
        self.assertEqual(floor_long - floor_short, 4 * diff_len)

    def test_diagnostics_lines_stuck_reserve_pinned_by_long_home_path(self):
        # A long home path (/var/run/kube-agents/long/home/path, len=35) scales the
        # irreducible floor to 590 chars (113 header + (245 + 4*35) footer + 92 omission line).
        # diagnostics_lines(home, max_chars=2000) with 20 error diagnostics (each 69 chars)
        # and 3 distinct stuck cards must derive its stuck reserve dynamically from home:
        # with dynamic reserve (590 chars), diag_budget is 1290 chars (18 lines + 99-char omission line = 1359),
        # leaving remaining = 641 >= 590, so the whole report stays <= 2000.
        # If the reserve were hardcoded to 500, diag_budget would take 19 lines (1428 chars),
        # leaving remaining = 572 < 590, causing render_stuck_cards to emit its 590-char floor
        # and blowing past 2000 (total = 2018 chars).
        home = Path("/var/run/kube-agents/long/home/path")
        entries = []
        for i in range(20):
            entries.append({
                "task_id": f"t_e_{i:02d}",
                "status": "failed",
                "diagnostics": [{
                    "kind": f"err_{i:02d}",
                    "severity": "error",
                    "title": f"Err {i:02d}",
                    "detail": "x" * 33,
                }],
            })
        for i in range(3):
            entries.append({
                "task_id": f"t_stuck_{i}",
                "status": "blocked",
                "title": f"Stuck {i}",
                "diagnostics": [{
                    "kind": "stuck_in_blocked",
                    "severity": "warning",
                    "data": {"age_hours": 24 + i},
                }],
            })
        payload = json.dumps(entries)
        runner = self.runner_for(0, payload)[0]
        lines = kbh.diagnostics_lines(home, "error", runner=runner, binary="/x/hermes", env={}, max_chars=2000)
        text = "\n".join(lines)
        self.assertLessEqual(len(text), 2000)

    def test_distinct_cards_agreeing_on_first_200_chars_not_merged_into_group(self):
        # Two cards whose titles exceed TITLE_MAX_CHARS (200) and share their first 200 characters,
        # differing only in their suffix (e.g., ' on pod-1' vs ' on pod-2').
        # Grouping must use the full normalized title so distinct incidents are not merged.
        prefix = "Triage incident " + ("x" * 190)
        payload = json.dumps([
            {"task_id": "t_inc_1", "status": "blocked", "title": prefix + " on pod-1",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 24}}]},
            {"task_id": "t_inc_2", "status": "blocked", "title": prefix + " on pod-2",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                              "title": "x", "detail": "y", "data": {"age_hours": 48}}]},
        ])
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, payload)[0],
                                      binary="/x/hermes", env={})
        text = "\n".join(lines)
        # Must not be rendered as a single group with '— 2 cards'
        self.assertNotIn("2 cards", text)
        # Both distinct task IDs are rendered with their own lines
        self.assertIn("t_inc_1", text)
        self.assertIn("t_inc_2", text)

    def test_diagnostics_omission_line_suppressed_when_diag_allowance_below_reserve(self):
        # When diag_allowance (max_chars - min_stuck_reserve) is below 120,
        # diagnostics omission line is not emitted so it does not uncharge over max_chars.
        cards = [{"task_id": f"t_err_{i}", "status": "failed",
                  "diagnostics": [{"kind": "repeated_failures", "severity": "error", "title": "Err", "detail": "x" * 50}]}
                 for i in range(2)]
        for i in range(20):
            cards.append({
                "task_id": f"t_stuck_{i}", "status": "blocked", "title": f"Stuck {i}",
                "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 24 + i}}],
            })
        payload = json.dumps(cards)
        runner = self.runner_for(0, payload)[0]
        # /opt/data floor is ~486 chars. With max_chars=520, diag_allowance is ~34 < 120.
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error", runner=runner, binary="/x/hermes", env={}, max_chars=520)
        text = "\n".join(lines)
        self.assertNotIn("other diagnostics omitted", text)

    def test_repeated_title_group_id_budget_sized_against_remaining_room(self):
        # A group's ids: line must be sized against room remaining when the group is reached,
        # not available_for_items, so a group whose head and latest: fit is not skipped in
        # favor of older single cards behind it.
        cards = [
            ("t_newest", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 1}},
             {"task_id": "t_newest", "status": "blocked", "title": "Fresh card"}),
        ]
        for i in range(30):
            cards.append((
                f"t_grp_{i:02d}",
                {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 2 + i}},
                {"task_id": f"t_grp_{i:02d}", "status": "blocked", "title": "Repeated title"}
            ))
        cards.append((
            "t_oldest",
            {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 50}},
            {"task_id": "t_oldest", "status": "blocked", "title": "Old card"}
        ))

        # max_chars=814 gives budget_for_body=420, available_for_items=300.
        # S (Fresh card, 50 chars) is taken. Remaining room = 250.
        # G (Repeated title, 30 cards) head + latest is 92 chars. Sizing ids: to remaining room
        # allows G to fit (243 chars total) and be taken.
        # Old card (50 chars) exceeds remaining room (7 chars) and is omitted.
        lines = kbh.render_stuck_cards(cards, {}, Path("/opt/data"), max_chars=814)
        text = "\n".join(lines)
        self.assertIn("Fresh card", text)
        self.assertIn("Repeated title", text)
        self.assertIn("latest: [t_grp_00]", text)
        self.assertNotIn("Old card", text)
        self.assertIn("... and 18 older stuck cards omitted", text)
        self.assertLessEqual(len(text), 814)




class ReportTests(unittest.TestCase):
    @staticmethod
    def clean_runner(argv, env, timeout):
        return 0, "[]", ""

    def test_missing_board_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(kbh.report(Path(d)), [])

    def test_healthy_board_prints_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            make_board(Path(d), tasks=[{"id": "t", "status": "done"}])
            lines = kbh.report(Path(d), runner=self.clean_runner,
                               binary="/x/hermes", env={})
            self.assertEqual(lines, [])

    def test_violation_and_diagnostic_are_both_reported(self):
        with tempfile.TemporaryDirectory() as d:
            make_board(Path(d),
                       tasks=[{"id": "t_ghost", "status": "ready",
                               "claim_lock": "host:9"}])
            payload = json.dumps([{"task_id": "t_ghost", "status": "ready",
                                   "diagnostics": [{"severity": "error",
                                                    "title": "Stranded",
                                                    "detail": ""}]}])
            lines = kbh.report(Path(d),
                               runner=lambda argv, env, timeout: (0, payload, ""),
                               binary="/x/hermes", env={})
            joined = "\n".join(lines)
            self.assertIn("Board invariant violations", joined)
            self.assertIn("ghost claim", joined)
            self.assertIn("Active kanban diagnostics", joined)

    def test_many_invariant_violations_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            tasks = [{"id": f"t_{i:03d}", "status": "blocked", "claim_lock": "ghost"} for i in range(50)]
            make_board(Path(d), tasks=tasks)
            lines = kbh.report(Path(d), runner=self.clean_runner, binary="/x/hermes", env={})
            text = "\n".join(lines)
            self.assertIn("Board invariant violations", text)
            self.assertIn("... and 30 other invariant violations omitted", text)
            self.assertLess(len(text), kbh.MAX_REPORT_CHARS)

    def test_report_viol_allowance_clamp_bounds_invariant_violations_under_tight_budget(self):
        # When report() is called with max_chars (e.g. 1200) where viol_allowance is below
        # MAX_DIAGNOSTIC_CHARS (3500), viol_budget clamps to viol_allowance - 120, taking only
        # what fits and emitting the omission line.
        with tempfile.TemporaryDirectory() as d:
            tasks = [{"id": f"t_ghost_{i:02d}", "status": "blocked", "claim_lock": "ghost"} for i in range(20)]
            make_board(Path(d), tasks=tasks)
            lines = kbh.report(Path(d), runner=self.clean_runner, binary="/x/hermes", env={}, max_chars=1200)
            text = "\n".join(lines)
            self.assertIn("... and 8 other invariant violations omitted", text)
            self.assertLessEqual(len(text) + 40, 1200)

    def test_report_viol_allowance_omission_line_suppressed_when_allowance_below_reserve(self):
        # When viol_allowance < 120 (e.g. max_chars=200 with 2 ghost claims), viol_budget is 0
        # and the omission line is suppressed because there is no budget to render it cleanly.
        with tempfile.TemporaryDirectory() as d:
            tasks = [{"id": f"t_ghost_{i:02d}", "status": "blocked", "claim_lock": "ghost"} for i in range(2)]
            make_board(Path(d), tasks=tasks)
            lines = kbh.report(Path(d), runner=self.clean_runner, binary="/x/hermes", env={}, max_chars=200)
            text = "\n".join(lines)
            self.assertNotIn("other invariant violation", text)

    def test_report_level_budget_accounting_with_violations_and_stuck_cards(self):
        # Invariant violations (~1500 chars) + diagnostics at cap (~3500 chars) + stuck cards.
        # report() must charge invariant violations, main() header reserve (40), and
        # diagnostics header reserve (27) against max_chars, passing remaining_chars to diagnostics_lines.
        with tempfile.TemporaryDirectory() as d:
            tasks = [{"id": f"t_ghost_{i:02d}", "status": "blocked", "claim_lock": "ghost"} for i in range(20)]
            make_board(Path(d), tasks=tasks)
            cards = []
            for i in range(20):
                cards.append({
                    "task_id": f"t_err_{i:02d}",
                    "status": "failed",
                    "diagnostics": [{
                        "kind": "repeated_failures",
                        "severity": "error",
                        "title": f"Err {i:02d}",
                        "detail": "x" * 150,
                    }],
                })
            for i in range(20):
                tid = f"t_stuck_{i:02d}"
                cards.append({
                    "task_id": tid,
                    "status": "blocked",
                    "title": f"Stuck card {i:02d}",
                    "diagnostics": [{
                        "kind": "stuck_in_blocked",
                        "severity": "warning",
                        "title": "x",
                        "detail": "y",
                        "data": {"age_hours": 24 + i},
                    }],
                })
            payload = json.dumps(cards)
            runner = lambda argv, env, timeout: (0, payload, "")

            # Make the fixture own the footer length by patching commands to {home}-free strings
            # so the test outcome is completely independent of tempdir path length.
            with unittest.mock.patch.object(kbh, "UNBLOCK_COMMAND", "hermes kanban unblock {task_id}"), \
                 unittest.mock.patch.object(kbh, "ARCHIVE_COMMAND", "hermes kanban archive {task_id}"):
                lines = kbh.report(Path(d), runner=runner, binary="/x/hermes", env={}, max_chars=3800)
                text = "\n".join(lines)
                # Both sections are present
                self.assertIn("Board invariant violations", text)
                self.assertIn("Active kanban diagnostics", text)
                # Whole report strictly respects max_chars including 40-char main() reserve
                self.assertLessEqual(len(text) + 40, 3800)
                # Stuck cards were cut under the tight budget, omitting later cards
                self.assertIn("older stuck cards omitted", text)

    def test_stuck_cards_omission_count_only_fallback_under_starved_budget(self):
        # When remaining budget for omission cannot fit IDs (max_ids_budget < 15),
        # render_stuck_cards falls back to count-only omission without '(ids:'.
        cards = [(f"t_stuck_{i:02d}", {"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 24 + i}},
                  {"task_id": f"t_stuck_{i:02d}", "status": "blocked", "title": f"Stuck {i:02d}"})
                 for i in range(20)]
        # At max_chars=500 (above the 486-char floor for /opt/data), output strictly respects max_chars
        lines = kbh.render_stuck_cards(cards, {}, Path("/opt/data"), max_chars=500)
        text = "\n".join(lines)
        self.assertLessEqual(len(text), 500)
        self.assertIn("older stuck cards omitted (run hermes kanban list", text)
        self.assertNotIn("(ids:", text)

        # Under extreme starvation below irreducible floor (~486 chars for /opt/data),
        # render_stuck_cards emits the floor rather than truncating operator commands.
        lines_floor = kbh.render_stuck_cards(cards, {}, Path("/opt/data"), max_chars=350)
        text_floor = "\n".join(lines_floor)
        self.assertEqual(len(text_floor), 486)
        self.assertIn("older stuck cards omitted (run hermes kanban list", text_floor)


class MainOutputContractTests(unittest.TestCase):
    """`no_agent` contract: stdout is the whole message, exit is always 0."""

    def _patch(self, attr, value):
        original = getattr(kbh, attr)
        setattr(kbh, attr, value)
        self.addCleanup(setattr, kbh, attr, original)

    def run_main(self, lines):
        self._patch("agent_home", lambda: Path("/nonexistent-home"))
        self._patch("report", lambda home, **kwargs: lines)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = kbh.main()
        return code, buffer.getvalue()

    def test_healthy_run_writes_nothing_at_all(self):
        self.assertEqual(self.run_main([]), (0, ""))

    def test_findings_are_printed_under_a_header(self):
        code, out = self.run_main(["  ghost claim: task t is 'ready'"])
        self.assertEqual(code, 0, "the message is the signal, not the exit code")
        self.assertIn("kanban board health: attention required", out)
        self.assertIn("ghost claim", out)

    def test_an_unreadable_board_reports_and_still_exits_zero(self):
        def boom(home, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        self._patch("agent_home", lambda: Path("/nonexistent-home"))
        self._patch("report", boom)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = kbh.main()
        self.assertEqual(code, 0)
        self.assertIn("cannot read", buffer.getvalue())


class BoardPathTests(unittest.TestCase):
    def test_board_comes_from_the_agent_home_not_hermes_home(self):
        # Under the platform roster HERMES_HOME is profiles/platform, which holds
        # no board; PLATFORM_AGENT_HOME is the volume root that does.
        with unittest.mock.patch.dict("os.environ", {"PLATFORM_AGENT_HOME": "/x", "HERMES_HOME": "/x/profiles/platform"}, clear=False):
            import os
            os.environ.pop(kbh.HOME_ENV, None)
            self.assertEqual(kbh.board_path(kbh.agent_home()), Path("/x/kanban.db"))

    def test_board_home_env_override(self):
        with unittest.mock.patch.dict("os.environ", {kbh.HOME_ENV: "/tmp/elsewhere"}, clear=False):
            self.assertEqual(kbh.board_path(kbh.agent_home()), Path("/tmp/elsewhere/kanban.db"))


class BlockedContextTests(unittest.TestCase):
    def test_kind_reason_title_and_assignee_are_read_from_the_board(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_board(
                Path(tmp),
                tasks=[{"id": "t_b", "status": "blocked", "title": "Triage x", "assignee": "cluster-a", "block_kind": None},
                       {"id": "t_d", "status": "done"}],
                events=[{"task_id": "t_b", "kind": "blocked", "payload": {"reason": "first", "kind": "needs_input"}, "created_at": 1},
                        {"task_id": "t_b", "kind": "blocked", "payload": {"reason": "judge rejected", "kind": "needs_input"}, "created_at": 2}],
            )
            conn = kbh.read_only_connection(db)
            try:
                context = kbh.blocked_context(conn)
            finally:
                conn.close()
        self.assertEqual(set(context), {"t_b"})
        self.assertEqual(context["t_b"]["kind"], "needs_input")
        self.assertEqual(context["t_b"]["reason"], "judge rejected")
        self.assertEqual(context["t_b"]["assignee"], "cluster-a")

    def test_kind_and_reason_both_follow_the_latest_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_board(
                Path(tmp),
                tasks=[{"id": "t_b", "status": "blocked", "block_kind": None}],
                events=[{"task_id": "t_b", "kind": "blocked", "payload": {"reason": "old", "kind": "needs_input"}, "created_at": 1},
                        {"task_id": "t_b", "kind": "blocked", "payload": {"reason": "new", "kind": "dependency"}, "created_at": 2}],
            )
            conn = kbh.read_only_connection(db)
            try:
                context = kbh.blocked_context(conn)
            finally:
                conn.close()
        self.assertEqual((context["t_b"]["kind"], context["t_b"]["reason"]), ("dependency", "new"))

    def test_an_unmigrated_board_yields_no_context_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "kanban.db"
            conn = sqlite3.connect(db)
            conn.executescript("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL);")
            conn.commit(); conn.close()
            conn = kbh.read_only_connection(db)
            try:
                self.assertEqual(kbh.blocked_context(conn), {})
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
