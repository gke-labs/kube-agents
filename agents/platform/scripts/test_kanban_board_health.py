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
from datetime import datetime, timezone
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

    def test_stuck_in_blocked_is_collected_under_the_error_floor_not_rendered(self):
        stuck = []
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, STUCK_PAYLOAD)[0],
                                      binary="/x/hermes", env={}, stuck=stuck)
        self.assertEqual(lines, [])
        self.assertEqual([item[0] for item in stuck], ["t_stuck"])

    def test_always_report_can_be_narrowed_by_env(self):
        stuck = []
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, STUCK_PAYLOAD)[0],
                                      binary="/x/hermes", env={}, always=frozenset(), stuck=stuck)
        self.assertEqual((lines, stuck), ([], []))
        self.assertEqual(kbh.always_report_kinds({"KANBAN_HEALTH_ALWAYS_REPORT": "a, b"}), frozenset({"a", "b"}))
        self.assertEqual(kbh.always_report_kinds({}), kbh.ALWAYS_REPORT_KINDS)

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

    def test_non_string_task_id_and_null_status_do_not_raise(self):
        # Malformed CLI output (task_id: null or an int, status: null) is
        # coerced rather than raising, for stuck cards and the other findings.
        cards = [
            {"task_id": None, "status": None, "title": "Shared task title",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10}}]},
            {"task_id": 12345, "status": "blocked", "title": "Shared task title",
             "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning", "data": {"age_hours": 10}}]},
            {"task_id": None, "status": "failed",
             "diagnostics": [{"kind": "repeated_failures", "severity": "error",
                              "title": "Failure without id", "detail": "some detail"}]},
        ]
        stuck = []
        lines = kbh.diagnostics_lines(Path("/opt/data"), "error",
                                      runner=self.runner_for(0, json.dumps(cards))[0],
                                      binary="/x/hermes", env={}, stuck=stuck)
        self.assertIn("Failure without id", "\n".join(lines))
        tasks = kbh.stuck_tasks(stuck, {})
        self.assertEqual(sorted(t["task_id"] for t in tasks), ["12345", "?"])
        text = "\n".join(kbh.render_stuck_tasks(tasks))
        self.assertIn("**Shared task title** (2 tasks)", text)


def stuck(task_id, title, age_hours=72.0, person=False):
    return {"task_id": task_id, "title": title, "person": person, "age_hours": age_hours}


class StuckTasksTests(unittest.TestCase):
    """The stuck tasks in plain words: what the channel reads."""

    def test_the_mock_s_two_tasks(self):
        lines = kbh.render_stuck_tasks([
            stuck("t_19d04f3c", "Upgrade seeded-b node pool to 1.33", 6 * 24 + 2, person=True),
            stuck("t_7c2e91ab", "Investigate payments-api crash-loop on seeded-a", 3 * 24 + 5),
        ])
        self.assertEqual(lines, [
            "**2 tasks are stuck and need someone to decide.**",
            "",
            "**Investigate payments-api crash-loop on seeded-a**",
            "Stopped 3 days ago before finishing.",
            "",
            "**Upgrade seeded-b node pool to 1.33**",
            "Paused 6 days ago.",
        ])

    def test_one_task_reads_singular(self):
        lines = kbh.render_stuck_tasks([stuck("t_1", "Triage x", 25)])
        self.assertEqual(lines[0], "**1 task is stuck and needs someone to decide.**")
        self.assertEqual(lines[-1], "Stopped 1 day ago before finishing.")

    def test_nothing_stuck_renders_nothing(self):
        self.assertEqual(kbh.render_stuck_tasks([]), [])

    def test_no_ids_reasons_or_commands_reach_the_text(self):
        context = {"t_7c2e91ab": {"title": "Investigate payments-api", "assignee": "platform",
                                  "kind": "capability", "reason": "worker exited before completing: kubectl"}}
        items = [("t_7c2e91ab", {"data": {"age_hours": 80}}, {})]
        text = "\n".join(kbh.render_stuck_tasks(kbh.stuck_tasks(items, context)))
        for absent in ("t_7c2e91ab", "worker exited", "kubectl", "hermes", "HERMES_HOME", "platform", "capability"):
            self.assertNotIn(absent, text)
        self.assertIn("**Investigate payments-api**", text)

    def test_only_needs_input_reads_paused(self):
        context = {"a": {"title": "A", "kind": "needs_input"}, "b": {"title": "B", "kind": "transient"},
                   "c": {"title": "C", "kind": None}}
        items = [(tid, {"data": {"age_hours": 30}}, {}) for tid in "abc"]
        tasks = {t["task_id"]: t["person"] for t in kbh.stuck_tasks(items, context)}
        self.assertEqual(tasks, {"a": True, "b": False, "c": False})

    def test_the_board_title_wins_and_the_cli_title_is_the_fallback(self):
        items = [("t_1", {"data": {"age_hours": 30}}, {"title": "From the CLI"}),
                 ("t_2", {"data": {"age_hours": 30}}, {"title": "From the CLI"})]
        tasks = kbh.stuck_tasks(items, {"t_1": {"title": "From the board"}})
        self.assertEqual([t["title"] for t in tasks], ["From the board", "From the CLI"])

    def test_ages(self):
        self.assertEqual(kbh.plain_age(0.5), "0 hours")
        self.assertEqual(kbh.plain_age(1.9), "1 hour")
        self.assertEqual(kbh.plain_age(23.9), "23 hours")
        self.assertEqual(kbh.plain_age(24), "1 day")
        self.assertEqual(kbh.plain_age(24 * 9 + 23), "9 days")
        self.assertIsNone(kbh.plain_age(float("inf")))
        self.assertIsNone(kbh.plain_age(float("nan")))
        self.assertIsNone(kbh.plain_age(float("-inf")))
        self.assertEqual(kbh.status_line(False, float("inf")), "Stopped before finishing.")
        self.assertEqual(kbh.status_line(True, float("inf")), "Paused.")

    def test_rows_are_newest_first(self):
        lines = kbh.render_stuck_tasks([stuck("o", "Old", 200), stuck("n", "New", 30), stuck("m", "Mid", 100)])
        self.assertEqual([ln for ln in lines if ln.startswith("**") and "stuck" not in ln],
                         ["**New**", "**Mid**", "**Old**"])

    def test_tasks_sharing_a_title_are_one_row_aged_by_the_oldest(self):
        lines = kbh.render_stuck_tasks([stuck("a", "Same", 30), stuck("b", "Same", 100), stuck("c", "Same", 50)])
        self.assertEqual(lines, [
            "**3 tasks are stuck and need someone to decide.**",
            "",
            "**Same** (3 tasks)",
            "Stopped 4 days ago before finishing.",
        ])

    def test_a_shared_title_waiting_on_a_person_and_not_stays_two_rows(self):
        lines = kbh.render_stuck_tasks([stuck("a", "Same", 30), stuck("b", "Same", 30, person=True)])
        self.assertEqual(lines.count("**Same**"), 2)

    def test_rows_past_the_cap_are_counted(self):
        tasks = [stuck(f"t_{i:02d}", f"Task {i:02d}", 30 + i) for i in range(kbh.MAX_STUCK_ROWS + 3)]
        lines = kbh.render_stuck_tasks(tasks)
        self.assertEqual(lines[0], f"**{kbh.MAX_STUCK_ROWS + 3} tasks are stuck and need someone to decide.**")
        self.assertIn(f"**Task {kbh.MAX_STUCK_ROWS - 1:02d}**", lines)
        self.assertNotIn(f"**Task {kbh.MAX_STUCK_ROWS:02d}**", lines)
        self.assertEqual(lines[-1], "And 3 more.")

    def test_the_worst_case_card_fits_its_reserve(self):
        tasks = [
            dict(stuck(f"t_{i}", f"{i:03d} " + "word " * 100, 1e6), held=["held " * 60, "other"])
            for i in range(kbh.MAX_STUCK_ROWS * 4)
        ]
        tasks += [stuck(f"g_{i}", "group", 1e6) for i in range(999)]
        text = "\n".join(kbh.render_stuck_tasks(tasks))
        self.assertLess(len(text), kbh.STUCK_CARD_RESERVE)

    def test_a_long_title_is_cut_at_a_word_and_emphasis_is_dropped(self):
        title = kbh.plain_title("**Bold** " + "alpha " * 40)
        self.assertLessEqual(len(title), kbh.STUCK_TITLE_MAX_CHARS)
        self.assertTrue(title.endswith("alpha…"))
        self.assertNotIn("*", title)
        self.assertEqual(kbh.plain_title("x" * 300), "x" * (kbh.STUCK_TITLE_MAX_CHARS - 1) + "…")
        self.assertEqual(kbh.plain_title("  "), "Untitled task")

    def test_a_stuck_task_names_the_todo_cards_it_holds_back(self):
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
        tasks = kbh.stuck_tasks([("t_stuck", {"data": {"age_hours": 30}}, {})], context)
        lines = kbh.render_stuck_tasks(tasks)
        self.assertIn("Holding back: Open GitOps PR for x.", lines)
        self.assertFalse(any("t_pr" in line for line in lines))

    def test_the_held_back_line_names_one_and_counts_the_rest(self):
        task = dict(stuck("a", "Triage x"), held=["One", "Two", "Three"])
        self.assertIn("Holding back: One and 2 more.", kbh.render_stuck_tasks([task]))

    def test_a_short_word_before_a_long_token_keeps_the_hard_cut(self):
        title = kbh.plain_title("Investigate " + "x" * 200)
        self.assertEqual(title, ("Investigate " + "x" * 200)[: kbh.STUCK_TITLE_MAX_CHARS - 1] + "…")

    def test_titles_that_read_the_same_share_one_row(self):
        lines = kbh.render_stuck_tasks([stuck("a", "Fix *payments*"), stuck("b", "Fix payments")])
        self.assertEqual(lines.count("**Fix payments** (2 tasks)"), 1)


class StuckDueTests(unittest.TestCase):
    """Said when a task first gets stuck, then weekly while any stays stuck."""

    DAY = 24 * 3600

    def test_a_first_stuck_task_is_said(self):
        due, kept = kbh.stuck_due([stuck("a", "A")], {}, 1000.0)
        self.assertTrue(due)
        self.assertEqual(kept, {"said": ["a"], "said_at": 1000.0})

    def test_the_next_days_are_quiet(self):
        state = {"said": ["a"], "said_at": 1000.0}
        for day in range(1, 7):
            due, kept = kbh.stuck_due([stuck("a", "A")], state, 1000.0 + day * self.DAY)
            self.assertFalse(due, day)
            self.assertEqual(kept, state)

    def test_a_week_later_it_is_said_again(self):
        due, kept = kbh.stuck_due([stuck("a", "A")], {"said": ["a"], "said_at": 1000.0}, 1000.0 + 7 * self.DAY)
        self.assertTrue(due)
        self.assertEqual(kept["said_at"], 1000.0 + 7 * self.DAY)

    def test_a_daily_run_a_few_minutes_short_of_the_week_still_reminds(self):
        week = 7 * self.DAY
        state = {"said": ["a"], "said_at": 1000.0}
        due, _ = kbh.stuck_due([stuck("a", "A")], state, 1000.0 + week - kbh.REMIND_SLACK_SECONDS)
        self.assertTrue(due)
        due, _ = kbh.stuck_due([stuck("a", "A")], state, 1000.0 + week - kbh.REMIND_SLACK_SECONDS - 1)
        self.assertFalse(due)

    def test_a_catch_up_post_hours_late_still_reminds_on_the_seventh_day(self):
        posted = 1000.0 + 4 * 3600  # a catch-up four hours after the usual slot
        due, _ = kbh.stuck_due([stuck("a", "A")], {"said": ["a"], "said_at": posted}, 1000.0 + 7 * self.DAY)
        self.assertTrue(due)

    def test_a_said_at_ahead_of_the_clock_does_not_silence_the_tasks(self):
        now = 1000.0
        due, kept = kbh.stuck_due([stuck("a", "A")], {"said": ["a"], "said_at": now + 3 * self.DAY}, now)
        self.assertTrue(due)
        self.assertEqual(kept["said_at"], now)

    def test_a_newly_stuck_task_is_said_with_the_rest_and_restarts_the_week(self):
        now = 1000.0 + 2 * self.DAY
        due, kept = kbh.stuck_due([stuck("a", "A"), stuck("b", "B")], {"said": ["a"], "said_at": 1000.0}, now)
        self.assertTrue(due)
        self.assertEqual(kept, {"said": ["a", "b"], "said_at": now})

    def test_an_unstuck_task_drops_out_so_it_is_new_if_it_sticks_again(self):
        due, kept = kbh.stuck_due([stuck("a", "A")], {"said": ["a", "b"], "said_at": 1000.0}, 2000.0)
        self.assertFalse(due)
        self.assertEqual(kept, {"said": ["a"], "said_at": 1000.0})
        due, _ = kbh.stuck_due([stuck("a", "A"), stuck("b", "B")], kept, 3000.0)
        self.assertTrue(due)

    def test_nothing_stuck_clears_the_record(self):
        self.assertEqual(kbh.stuck_due([], {"said": ["a"], "said_at": 1000.0}, 2000.0),
                         (False, {"said": [], "said_at": None}))

    def test_state_round_trips_and_a_bad_record_reads_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cron" / "state.json"
            self.assertEqual(kbh.load_state(path), {"said": [], "said_at": None})
            kbh.save_state(path, {"said": ["a"], "said_at": 5.0})
            self.assertEqual(kbh.load_state(path), {"said": ["a"], "said_at": 5.0})
            path.write_text("{not json")
            self.assertEqual(kbh.load_state(path), {"said": [], "said_at": None})
            path.write_text(json.dumps({"version": 99, "said": ["a"], "said_at": 5.0}))
            self.assertEqual(kbh.load_state(path), {"said": [], "said_at": None})
            path.write_text('{"version": 1, "said": ["a"], "said_at": NaN}')
            self.assertEqual(kbh.load_state(path), {"said": ["a"], "said_at": None})

    def _store(self, home, error, run_at):
        store = Path(home) / "profiles" / "platform" / "cron" / "jobs.json"
        store.parent.mkdir(parents=True)
        entry = {"id": "kanban-board-health", "last_run_at": run_at, "last_delivery_error": error}
        store.write_text(json.dumps({"jobs": [entry]}))

    def test_a_failed_delivery_after_the_post_unsays_it(self):
        said_at = 1_761_220_000.0  # 2025-10-23T11:46:40Z
        with tempfile.TemporaryDirectory() as d:
            self._store(d, "composed but not delivered to slack", "2025-10-23T11:47:10+00:00")
            self.assertTrue(kbh.last_post_undelivered(Path(d), said_at, env={}))

    def test_a_delivered_post_or_an_older_failure_stands(self):
        said_at = 1_761_220_000.0
        with tempfile.TemporaryDirectory() as d:
            self._store(d, None, "2025-10-23T11:47:10+00:00")
            self.assertFalse(kbh.last_post_undelivered(Path(d), said_at, env={}))
        with tempfile.TemporaryDirectory() as d:
            self._store(d, "composed but not delivered to slack", "2025-10-22T11:47:10+00:00")
            self.assertFalse(kbh.last_post_undelivered(Path(d), said_at, env={}))
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(kbh.last_post_undelivered(Path(d), said_at, env={}))

    def test_an_unwritable_record_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = Path(d) / "file"
            blocker.write_text("")
            kbh.save_state(blocker / "state.json", {"said": [], "said_at": None})

    def test_state_path_defaults_beside_the_platform_ledgers(self):
        self.assertEqual(kbh.state_path(Path("/opt/data"), {}),
                         Path("/opt/data/profiles/platform/cron/kanban_board_health.json"))
        self.assertEqual(kbh.state_path(Path("/opt/data"), {"KANBAN_HEALTH_STATE": "/tmp/s.json"}),
                         Path("/tmp/s.json"))


class ReportTests(unittest.TestCase):
    @staticmethod
    def clean_runner(argv, env, timeout):
        return 0, "[]", ""

    def test_missing_board_is_silent(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(kbh.report(Path(d)), ([], []))

    def test_healthy_board_prints_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            make_board(Path(d), tasks=[{"id": "t", "status": "done"}])
            self.assertEqual(kbh.report(Path(d), runner=self.clean_runner, binary="/x/hermes", env={}), ([], []))

    def test_stuck_tasks_come_back_with_the_board_s_context(self):
        with tempfile.TemporaryDirectory() as d:
            make_board(Path(d), tasks=[{"id": "t_stuck", "status": "blocked", "title": "Triage x",
                                        "block_kind": "needs_input"}])
            tasks, lines = kbh.report(Path(d), runner=lambda argv, env, timeout: (0, STUCK_PAYLOAD, ""),
                                      binary="/x/hermes", env={})
        self.assertEqual(lines, [])
        self.assertEqual(tasks, [{"task_id": "t_stuck", "title": "Triage x", "person": True, "age_hours": 456.2, "held": []}])

    def test_an_engine_that_did_not_answer_returns_no_stuck_tasks(self):
        with tempfile.TemporaryDirectory() as d:
            make_board(Path(d), tasks=[{"id": "t", "status": "blocked"}])
            for runner in (lambda argv, env, timeout: (2, "", "boom"),
                           lambda argv, env, timeout: (0, "not json", "")):
                tasks, lines = kbh.report(Path(d), runner=runner, binary="/x/hermes", env={})
                self.assertIsNone(tasks)
                self.assertIn("diagnostics unavailable", "\n".join(lines))
            def timeout(argv, env, timeout):
                raise TimeoutError("took too long")
            self.assertIsNone(kbh.report(Path(d), runner=timeout, binary="/x/hermes", env={})[0])
            with unittest.mock.patch.object(kbh, "hermes_bin", side_effect=FileNotFoundError("no hermes")):
                self.assertIsNone(kbh.report(Path(d), runner=self.clean_runner, env={})[0])

    def test_violation_and_diagnostic_are_both_reported(self):
        with tempfile.TemporaryDirectory() as d:
            make_board(Path(d),
                       tasks=[{"id": "t_ghost", "status": "ready",
                               "claim_lock": "host:9"}])
            payload = json.dumps([{"task_id": "t_ghost", "status": "ready",
                                   "diagnostics": [{"severity": "error",
                                                    "title": "Stranded",
                                                    "detail": ""}]}])
            _tasks, lines = kbh.report(Path(d),
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
            _tasks, lines = kbh.report(Path(d), runner=self.clean_runner, binary="/x/hermes", env={})
            text = "\n".join(lines)
            self.assertIn("Board invariant violations", text)
            self.assertIn("... and 30 other invariant violations omitted", text)
            self.assertLess(len(text) + kbh.STUCK_CARD_RESERVE, kbh.MAX_REPORT_CHARS)

    def test_report_viol_allowance_clamp_bounds_invariant_violations_under_tight_budget(self):
        with tempfile.TemporaryDirectory() as d:
            tasks = [{"id": f"t_ghost_{i:02d}", "status": "blocked", "claim_lock": "ghost"} for i in range(20)]
            make_board(Path(d), tasks=tasks)
            _tasks, lines = kbh.report(Path(d), runner=self.clean_runner, binary="/x/hermes", env={}, max_chars=1200)
            text = "\n".join(lines)
            self.assertIn("... and 8 other invariant violations omitted", text)
            self.assertLessEqual(len(text) + 40, 1200)

    def test_report_viol_allowance_omission_line_suppressed_when_allowance_below_reserve(self):
        with tempfile.TemporaryDirectory() as d:
            tasks = [{"id": f"t_ghost_{i:02d}", "status": "blocked", "claim_lock": "ghost"} for i in range(2)]
            make_board(Path(d), tasks=tasks)
            _tasks, lines = kbh.report(Path(d), runner=self.clean_runner, binary="/x/hermes", env={}, max_chars=200)
            self.assertNotIn("other invariant violation", "\n".join(lines))


class MainOutputContractTests(unittest.TestCase):
    """`no_agent` contract: stdout is the whole message, exit is always 0."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = {"KANBAN_HEALTH_STATE": str(Path(tmp.name) / "state.json")}

    def _patch(self, attr, value):
        original = getattr(kbh, attr)
        setattr(kbh, attr, value)
        self.addCleanup(setattr, kbh, attr, original)

    def run_main(self, tasks, lines, now=1000.0):
        self._patch("agent_home", lambda: Path("/nonexistent-home"))
        self._patch("report", lambda home, **kwargs: (tasks, lines))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = kbh.main(env=self.env, now=now)
        return code, buffer.getvalue()

    def test_healthy_run_writes_nothing_at_all(self):
        self.assertEqual(self.run_main([], []), (0, ""))

    def test_findings_are_printed_under_a_header(self):
        code, out = self.run_main([], ["  ghost claim: task t is 'ready'"])
        self.assertEqual(code, 0, "the message is the signal, not the exit code")
        self.assertIn("kanban board health: attention required", out)
        self.assertIn("ghost claim", out)

    def test_stuck_tasks_print_first_then_stay_quiet_until_the_week_is_up(self):
        tasks = [stuck("t_1", "Triage x", 30)]
        _code, first = self.run_main(tasks, [])
        self.assertEqual(first, "**1 task is stuck and needs someone to decide.**\n\n**Triage x**\n"
                                "Stopped 1 day ago before finishing.\n")
        self.assertEqual(self.run_main(tasks, [], now=1000.0 + 86400), (0, ""))
        self.assertEqual(self.run_main(tasks, [], now=1000.0 + 7 * 86400)[1], first)

    def test_an_engine_that_did_not_answer_leaves_the_record_alone(self):
        tasks = [stuck("t_1", "Triage x", 30)]
        self.run_main(tasks, [])
        path = Path(self.env["KANBAN_HEALTH_STATE"])
        before = path.read_text()
        code, out = self.run_main(None, ["Active kanban diagnostics:", "  diagnostics unavailable: boom"],
                                  now=1000.0 + 8 * 86400)
        self.assertEqual(code, 0)
        self.assertEqual(out, "kanban board health: attention required\nActive kanban diagnostics:\n"
                              "  diagnostics unavailable: boom\n")
        self.assertEqual(path.read_text(), before)
        # The next answer is still inside the week the record started, so stays quiet.
        self.assertEqual(self.run_main(tasks, [], now=1000.0 + 86400), (0, ""))

    def test_a_healthy_run_writes_no_record(self):
        self.run_main([], [])
        self.assertFalse(Path(self.env["KANBAN_HEALTH_STATE"]).exists())

    def test_a_card_that_fails_to_render_is_not_recorded_as_said(self):
        self._patch("render_stuck_tasks", unittest.mock.Mock(side_effect=ValueError("render")))
        with self.assertRaises(ValueError):
            self.run_main([stuck("t_1", "Triage x", 30)], [])
        self.assertFalse(Path(self.env["KANBAN_HEALTH_STATE"]).exists())

    def test_stuck_tasks_and_other_findings_print_together(self):
        _code, out = self.run_main([stuck("t_1", "Triage x", 30)], ["  ghost claim: task t is 'ready'"])
        card, other = out.split("\n\n**Triage x**\nStopped 1 day ago before finishing.\n\n")
        self.assertEqual(card, "**1 task is stuck and needs someone to decide.**")
        self.assertTrue(other.startswith("kanban board health: attention required\n"))

    def card(self):
        path = Path(self.env["KANBAN_HEALTH_STATE"]).with_suffix(".card.json")
        return json.loads(path.read_text()) if path.exists() else None

    def test_stuck_tasks_said_alone_leave_a_card_matching_the_post(self):
        tasks = [stuck("t_1", "Triage x", 30), stuck("t_2", "Upgrade pool", 2, person=True)]
        _code, out = self.run_main(tasks, [])
        card = self.card()
        self.assertEqual(card["text"], out.rstrip("\n"))
        self.assertEqual(card["headline"], "2 tasks are stuck and need someone to decide.")
        self.assertEqual(card["rows"], [
            {"title": "Upgrade pool", "count": 1, "status": "Paused 2 hours ago.", "held": "", "tasks": ["t_2"]},
            {"title": "Triage x", "count": 1, "status": "Stopped 1 day ago before finishing.", "held": "",
             "tasks": ["t_1"]},
        ])
        self.assertEqual(card["more"], "")

    def test_a_row_of_tasks_sharing_a_title_carries_every_id(self):
        self.run_main([stuck("t_1", "Triage x", 30), stuck("t_2", "Triage x", 5)], [])
        self.assertEqual([(r["count"], r["tasks"]) for r in self.card()["rows"]], [(2, ["t_1", "t_2"])])

    def test_a_quiet_run_removes_the_card(self):
        tasks = [stuck("t_1", "Triage x", 30)]
        self.run_main(tasks, [])
        self.assertIsNotNone(self.card())
        self.run_main(tasks, [], now=1000.0 + 86400)
        self.assertIsNone(self.card())

    def test_other_findings_post_as_text_with_no_card(self):
        self.run_main([stuck("t_1", "Triage x", 30)], ["  ghost claim: task t is 'ready'"])
        self.assertIsNone(self.card())
    def test_an_unreadable_board_removes_the_card(self):
        self.run_main([stuck("t_1", "Triage x", 30)], [])
        self.assertIsNotNone(self.card())

        def boom(home, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        self._patch("report", boom)
        with contextlib.redirect_stdout(io.StringIO()):
            kbh.main(env=self.env, now=1000.0 + 86400)
        self.assertIsNone(self.card())
    def test_an_unreadable_board_reports_and_still_exits_zero(self):
        def boom(home, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        self._patch("agent_home", lambda: Path("/nonexistent-home"))
        self._patch("report", boom)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = kbh.main(env=self.env)
        self.assertEqual(code, 0)
        self.assertIn("cannot read", buffer.getvalue())



class DeliveryFailureTests(unittest.TestCase):
    """`main` against a real agent home, so it reads the scheduler's store as on the pod."""

    T0 = 1_761_220_000.0
    DAY = 24 * 3600

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.store = self.home / "profiles" / "platform" / "cron" / "jobs.json"
        self.store.parent.mkdir(parents=True)
        self.stamp(None, self.T0)
        original = kbh.agent_home
        kbh.agent_home = lambda: self.home
        self.addCleanup(setattr, kbh, "agent_home", original)

    def stamp(self, error, at):
        run_at = datetime.fromtimestamp(at, tz=timezone.utc).isoformat()
        entry = {"id": "kanban-board-health", "last_run_at": run_at, "last_delivery_error": error}
        self.store.write_text(json.dumps({"jobs": [entry]}))

    def run_main(self, report, now):
        with unittest.mock.patch.object(kbh, "report", report):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                kbh.main(env={}, now=now)
        return buffer.getvalue()

    def answer(self, tasks):
        return lambda home, **kwargs: (tasks, [])

    def post_then_fail_delivery(self):
        self.assertIn("**Triage x**", self.run_main(self.answer([stuck("a", "Triage x")]), self.T0))
        self.stamp("composed but not delivered to slack", self.T0 + 30)

    def test_a_failed_delivery_says_the_tasks_again_through_main(self):
        self.post_then_fail_delivery()
        self.assertIn("**Triage x**", self.run_main(self.answer([stuck("a", "Triage x")]), self.T0 + self.DAY))

    def test_a_forgotten_post_is_written_down_on_a_day_nothing_is_stuck(self):
        self.post_then_fail_delivery()
        self.assertEqual(self.run_main(self.answer([]), self.T0 + self.DAY), "")
        self.stamp(None, self.T0 + self.DAY + 30)  # a run that printed nothing clears the stamp
        out = self.run_main(self.answer([stuck("a", "Triage x")]), self.T0 + 2 * self.DAY)
        self.assertIn("**Triage x**", out)

    def test_an_engine_failure_after_a_failed_delivery_still_forgets_the_post(self):
        def no_answer(home, **kwargs):
            return None, ["Active kanban diagnostics:", "  diagnostics unavailable: boom"]

        def unreadable(home, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        for name, failing in (("engine", no_answer), ("board", unreadable)):
            with self.subTest(failure=name):
                self.post_then_fail_delivery()
                self.run_main(failing, self.T0 + self.DAY)
                self.stamp(None, self.T0 + self.DAY + 30)  # that run's own post delivered
                out = self.run_main(self.answer([stuck("a", "Triage x")]), self.T0 + 2 * self.DAY)
                self.assertIn("**Triage x**", out)
                (self.home / "profiles" / "platform" / "cron" / "kanban_board_health.json").unlink()

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
    def test_kind_and_title_are_read_from_the_board(self):
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
        self.assertEqual(context, {"t_b": {"title": "Triage x", "kind": "needs_input"}})

    def test_kind_follows_the_latest_event(self):
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
        self.assertEqual(context["t_b"]["kind"], "dependency")

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
