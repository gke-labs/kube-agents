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
    block_kind TEXT
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
"""

STUCK_PAYLOAD = json.dumps([{
    "task_id": "t_stuck", "status": "blocked",
    "diagnostics": [{"kind": "stuck_in_blocked", "severity": "warning",
                     "title": "Task has been blocked for 456h", "detail": "…",
                     "data": {"blocked_at": 1, "age_hours": 456.2}}],
}])


def make_board(tmp: Path, tasks=(), runs=(), events=()):
    db = tmp / "kanban.db"
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)
    for row in tasks:
        conn.execute(
            "INSERT INTO tasks (id,title,status,assignee,created_at,claim_lock,current_run_id,block_kind)"
            " VALUES (:id,:title,:status,:assignee,:created_at,:claim_lock,:current_run_id,:block_kind)",
            {"title": "t", "assignee": "platform", "created_at": 1000,
             "claim_lock": None, "current_run_id": None, "block_kind": None, **row},
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
