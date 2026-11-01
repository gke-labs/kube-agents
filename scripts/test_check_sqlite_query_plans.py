"""Tests for scripts/check_sqlite_query_plans.py."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.check_sqlite_query_plans import (
    BOUNDED_TABLES,
    DEFAULT_BASELINE,
    KIND_CLEAN,
    KIND_DYNAMIC,
    KIND_ERROR,
    KIND_SCAN,
    KIND_SCHEMA,
    KIND_SKIPPED,
    SQL_SOURCES,
    Statement,
    collect_sources,
    collect_statements,
    load_baseline,
    parse_baseline,
    plan_statements,
    render_report,
)

SCRIPT = _REPO_ROOT / "scripts" / "check_sqlite_query_plans.py"

# The issue's acceptance bound.
ACCEPTANCE_SECONDS = 30

# The fixture: one unbounded table with an index on `a` and none on `b`.
FIXTURE_SCHEMA = """
CREATE TABLE events (id INTEGER PRIMARY KEY, a TEXT, b TEXT);
CREATE INDEX events_a ON events (a);
"""
INDEXED_QUERY = "SELECT id FROM events WHERE a = ?"
UNINDEXED_QUERY = "SELECT id FROM events WHERE b = ?"
ALIASED_UNINDEXED_QUERY = "SELECT e.id FROM events e WHERE e.b = ?"
HERMES_QUERY = "SELECT id FROM sessions WHERE parent_session_id = ?"
BROKEN_QUERY = "SELECT nope FROM events WHERE a = ?"
DDL = "CREATE INDEX IF NOT EXISTS events_b ON events (b)"


def fixture_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(FIXTURE_SCHEMA)
    return conn


def statements(*sqls: str) -> list[Statement]:
    return [Statement("fixture.py", index + 1, sql) for index, sql in enumerate(sqls)]


class ProductionRunTest(unittest.TestCase):
    """The check as CI runs it: head passes, within the bound, offline."""

    def test_head_passes_with_no_new_scan_and_no_stale_entry(self):
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=ACCEPTANCE_SECONDS * 2,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("sqlite query plans: OK", proc.stdout)
        self.assertNotIn("FAIL:", proc.stdout)
        self.assertLess(elapsed, ACCEPTANCE_SECONDS, f"the check took {elapsed:.1f}s")

    def test_roster_yields_planned_statements_from_every_file(self):
        found = collect_sources()
        for relative in SQL_SOURCES:
            self.assertTrue(any(s.path == relative for s in found), f"no SQL collected from {relative}")
        # The server's own tables are what the gate is for; a run that collects
        # none of their statements has lost the extractor, not the scans.
        self.assertTrue(any("intercepted_events" in s.sql for s in found))
        self.assertTrue(any("findings" in s.sql for s in found))

    def test_baseline_parses_and_every_entry_names_a_reason(self):
        baseline = load_baseline(DEFAULT_BASELINE)
        self.assertTrue(baseline)
        for statement, reason in baseline.items():
            self.assertTrue(reason, statement)


class PlannerTest(unittest.TestCase):
    def test_unindexed_query_fails_with_the_plan_line_in_the_output(self):
        conn = fixture_conn()
        report = plan_statements(conn, statements(UNINDEXED_QUERY), baseline={}, bounded={})
        self.assertFalse(report.ok)
        self.assertEqual([r.kind for r in report.results], [KIND_SCAN])
        output = render_report(report, {})
        self.assertIn("SCAN events", output)
        self.assertIn("fixture.py:1", output)
        self.assertIn(UNINDEXED_QUERY, output)
        self.assertIn("FAIL: full scans not in the baseline", output)

    def test_indexed_query_is_clean(self):
        conn = fixture_conn()
        report = plan_statements(conn, statements(INDEXED_QUERY), baseline={}, bounded={})
        self.assertTrue(report.ok)
        self.assertEqual([r.kind for r in report.results], [KIND_CLEAN])

    def test_scan_through_an_alias_is_still_a_scan(self):
        conn = fixture_conn()
        report = plan_statements(conn, statements(ALIASED_UNINDEXED_QUERY), baseline={}, bounded={})
        self.assertFalse(report.ok)
        self.assertEqual(report.new_scans[0][1][0].table, "events")

    def test_scan_on_a_bounded_table_passes(self):
        conn = fixture_conn()
        report = plan_statements(conn, statements(UNINDEXED_QUERY), baseline={}, bounded={"events": "fixture"})
        self.assertTrue(report.ok)
        self.assertEqual(len(report.allowed_scans), 1)
        self.assertIn("scans on bounded tables (allowed)", render_report(report, {}))

    def test_baselined_scan_passes_and_is_listed_with_its_reason(self):
        conn = fixture_conn()
        baseline = {UNINDEXED_QUERY: "issue #0 owns the index"}
        report = plan_statements(conn, statements(UNINDEXED_QUERY), baseline=baseline, bounded={})
        self.assertTrue(report.ok)
        self.assertEqual(len(report.baselined_scans), 1)
        self.assertIn("issue #0 owns the index", render_report(report, baseline))

    def test_baseline_entry_that_stops_scanning_fails(self):
        conn = fixture_conn()
        conn.execute(DDL)
        baseline = {UNINDEXED_QUERY: "stale"}
        report = plan_statements(conn, statements(UNINDEXED_QUERY), baseline=baseline, bounded={})
        self.assertFalse(report.ok)
        self.assertEqual(report.stale_baseline, [UNINDEXED_QUERY])
        self.assertIn("FAIL: baseline entries that no longer scan", render_report(report, baseline))

    def test_baseline_matches_on_whitespace_normalised_text(self):
        conn = fixture_conn()
        spread = Statement("fixture.py", 1, "SELECT id\n    FROM events\n    WHERE b = ?")
        report = plan_statements(conn, [spread], baseline={UNINDEXED_QUERY: "known"}, bounded={})
        self.assertTrue(report.ok)

    def test_statement_on_a_table_without_ddl_is_skipped_naming_the_table(self):
        conn = fixture_conn()
        report = plan_statements(conn, statements(HERMES_QUERY), baseline={}, bounded={})
        self.assertTrue(report.ok)
        result = report.results[0]
        self.assertEqual(result.kind, KIND_SKIPPED)
        self.assertEqual(result.message, "sessions")
        self.assertIn("table sessions", render_report(report, {}))

    def test_plan_error_on_an_owned_table_fails(self):
        conn = fixture_conn()
        report = plan_statements(conn, statements(BROKEN_QUERY), baseline={}, bounded={})
        self.assertFalse(report.ok)
        self.assertEqual(report.results[0].kind, KIND_ERROR)
        self.assertIn("no such column: nope", render_report(report, {}))

    def test_ddl_is_listed_not_planned(self):
        conn = fixture_conn()
        report = plan_statements(conn, statements(DDL, "PRAGMA table_info(events)", "BEGIN IMMEDIATE"), baseline={}, bounded={})
        self.assertTrue(report.ok)
        self.assertEqual({r.kind for r in report.results}, {KIND_SCHEMA})

    def test_unparseable_statement_is_dynamic(self):
        conn = fixture_conn()
        report = plan_statements(conn, statements("UPDATE events SET ? WHERE id = ?"), baseline={}, bounded={})
        self.assertTrue(report.ok)
        self.assertEqual(report.results[0].kind, KIND_DYNAMIC)
        self.assertIn("dynamic: not SQL as written", render_report(report, {}))

    def test_subquery_scan_is_one_entry_for_the_statement(self):
        conn = fixture_conn()
        nested = "SELECT id FROM events WHERE b = ? OR id IN (SELECT id FROM events WHERE b = ?)"
        report = plan_statements(conn, statements(nested), baseline={}, bounded={})
        self.assertEqual(len(report.new_scans), 1)
        self.assertEqual(len(report.new_scans[0][1]), 2)

    def test_bounded_tables_are_the_small_ones(self):
        for table in ("intercepted_events", "session_metadata", "incidents", "findings", "findings_additions"):
            self.assertNotIn(table, BOUNDED_TABLES)


class ExtractionTest(unittest.TestCase):
    SOURCE = textwrap.dedent(
        '''
        """SELECT nothing FROM this docstring."""
        import os

        TABLE_REASON = "drift"
        PENDING = 1
        _SELECT = f"SELECT {', '.join(COLUMNS)} FROM findings"
        LIMIT = os.getenv("LIMIT")


        def query(conn, sid):
            """UPDATE nothing FROM this docstring either."""
            conn.execute(
                "SELECT id FROM events "
                "WHERE a = ?"
            )
            conn.execute(f"SELECT id FROM events WHERE reason = '{TABLE_REASON}' AND backlog = {PENDING}")
            conn.execute(f"SELECT id FROM events WHERE id = {sid}")
            conn.execute("DELETE FROM events " + "WHERE b = ?")
            conn.execute(f"{_SELECT} WHERE id = ?")
            conn.execute("UPDATE events SET {cols} WHERE id = ?".format(cols="a = 1"))
            conn.execute(f"SELECT id FROM events LIMIT {LIMIT}")
            logger.info("Select the session first")
            return "SELECT"
        '''
    )

    def setUp(self):
        self.found = collect_statements("fixture.py", self.SOURCE)
        self.sqls = [s.sql for s in self.found]

    def test_docstrings_and_prose_are_skipped(self):
        self.assertNotIn("SELECT nothing FROM this docstring.", self.sqls)
        self.assertNotIn("UPDATE nothing FROM this docstring either.", self.sqls)
        self.assertNotIn("Select the session first", self.sqls)
        self.assertNotIn("SELECT", self.sqls)

    def test_implicit_concatenation_is_one_statement(self):
        self.assertIn("SELECT id FROM events WHERE a = ?", self.sqls)

    def test_module_constants_are_substituted(self):
        self.assertIn("SELECT id FROM events WHERE reason = 'drift' AND backlog = 1", self.sqls)

    def test_local_placeholder_becomes_a_parameter(self):
        self.assertIn("SELECT id FROM events WHERE id = ?", self.sqls)

    def test_explicit_concatenation_is_one_statement(self):
        self.assertIn("DELETE FROM events WHERE b = ?", self.sqls)

    def test_constant_fragment_is_expanded(self):
        self.assertIn("SELECT ? FROM findings WHERE id = ?", self.sqls)

    def test_format_string_and_unresolved_constant_stay_as_written(self):
        self.assertIn("UPDATE events SET {cols} WHERE id = ?", self.sqls)
        self.assertIn("SELECT id FROM events LIMIT ?", self.sqls)

    def test_lines_point_at_the_statement(self):
        by_sql = {s.sql: s.line for s in self.found}
        source_lines = self.SOURCE.splitlines()
        self.assertIn("DELETE FROM events", source_lines[by_sql["DELETE FROM events WHERE b = ?"] - 1])


class BaselineParseTest(unittest.TestCase):
    def test_reason_lines_belong_to_the_entry_below(self):
        text = textwrap.dedent(
            """
            # file header

            # first reason
            # second line
            SELECT a FROM t

            # other
            DELETE FROM t WHERE x = ?
            """
        )
        self.assertEqual(
            parse_baseline(text),
            {"SELECT a FROM t": "first reason second line", "DELETE FROM t WHERE x = ?": "other"},
        )

    def test_entry_without_a_reason_is_refused(self):
        with self.assertRaises(ValueError):
            parse_baseline("SELECT a FROM t\n")

    def test_duplicate_entry_is_refused(self):
        with self.assertRaises(ValueError):
            parse_baseline("# r\nSELECT a FROM t\n\n# r\nSELECT a FROM t\n")

    def test_missing_file_is_an_empty_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_baseline(Path(tmp) / "absent.txt"), {})


if __name__ == "__main__":
    unittest.main()
