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
    BaselineEntry,
    Statement,
    collect_discovered,
    collect_sources,
    collect_statements,
    discover_sources,
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
FIXTURE_PATH = "fixture.py"
INDEXED_QUERY = "SELECT id FROM events WHERE a = ?"
UNINDEXED_QUERY = "SELECT id FROM events WHERE b = ?"
ALIASED_UNINDEXED_QUERY = "SELECT e.id FROM events e WHERE e.b = ?"
HERMES_QUERY = "SELECT id FROM sessions WHERE parent_session_id = ?"
MISSPELT_QUERY = "SELECT id FROM event WHERE a = ?"
BROKEN_QUERY = "SELECT nope FROM events WHERE a = ?"
DYNAMIC_OWNED = "UPDATE events SET ? WHERE id = ?"
DYNAMIC_FOREIGN = "SELECT id FROM tasks WHERE id IN ({marks})"
DDL = "CREATE INDEX IF NOT EXISTS events_b ON events (b)"
FOREIGN = frozenset({"sessions", "tasks"})


def fixture_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(FIXTURE_SCHEMA)
    return conn


def statements(*sqls: str, path: str = FIXTURE_PATH) -> list[Statement]:
    return [Statement(path, index + 1, sql) for index, sql in enumerate(sqls)]


def baseline(*entries: tuple[str, str] | tuple[str, str, int]) -> dict:
    return {(path_sql[0], path_sql[1]): BaselineEntry("known", *path_sql[2:]) for path_sql in entries}


def plan(conn, stmts, base=None, bounded=None):
    return plan_statements(conn, stmts, baseline=base or {}, bounded=bounded or {}, foreign=FOREIGN)


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
        # Discovery reaches the other writers of the server's tables: the chat
        # agent's session-store plugin and the admin console's in-pod programs.
        self.assertIn("agents/chat/defaults/plugins/session_store/store.py", proc.stdout)
        self.assertIn("admin_console/agent_runtime.py", proc.stdout)

    def test_roster_yields_planned_statements_from_every_file(self):
        found = collect_sources()
        for relative in SQL_SOURCES:
            self.assertTrue(any(s.path == relative for s in found), f"no SQL collected from {relative}")
        # The server's own tables are what the gate is for; a run that collects
        # none of their statements has lost the extractor, not the scans.
        self.assertTrue(any("intercepted_events" in s.sql for s in found))
        self.assertTrue(any("findings" in s.sql for s in found))

    def test_baseline_parses_and_every_entry_names_a_reason_and_an_existing_file(self):
        entries = load_baseline(DEFAULT_BASELINE)
        self.assertTrue(entries)
        for (path, statement), entry in entries.items():
            self.assertTrue(entry.reason, statement)
            self.assertTrue((_REPO_ROOT / path).is_file(), path)


class PlannerTest(unittest.TestCase):
    def test_unindexed_query_fails_with_the_plan_line_in_the_output(self):
        report = plan(fixture_conn(), statements(UNINDEXED_QUERY))
        self.assertFalse(report.ok)
        self.assertEqual([r.kind for r in report.results], [KIND_SCAN])
        output = render_report(report, {})
        self.assertIn("SCAN events", output)
        self.assertIn("fixture.py:1", output)
        self.assertIn(f"{FIXTURE_PATH} {UNINDEXED_QUERY}", output)
        self.assertIn("FAIL: full scans not in the baseline", output)

    def test_indexed_query_is_clean(self):
        report = plan(fixture_conn(), statements(INDEXED_QUERY))
        self.assertTrue(report.ok)
        self.assertEqual([r.kind for r in report.results], [KIND_CLEAN])

    def test_scan_through_an_alias_is_still_a_scan(self):
        report = plan(fixture_conn(), statements(ALIASED_UNINDEXED_QUERY))
        self.assertFalse(report.ok)
        self.assertEqual(report.unacknowledged[0].scans[0].table, "events")

    def test_scan_on_a_bounded_table_passes(self):
        report = plan(fixture_conn(), statements(UNINDEXED_QUERY), bounded={"events": "fixture"})
        self.assertTrue(report.ok)
        self.assertEqual(len(report.allowed_scans), 1)
        self.assertIn("scans on bounded tables (allowed)", render_report(report, {}))

    def test_baselined_scan_passes_and_is_listed_with_its_reason(self):
        base = {(FIXTURE_PATH, UNINDEXED_QUERY): BaselineEntry("issue #0 owns the index")}
        report = plan(fixture_conn(), statements(UNINDEXED_QUERY), base)
        self.assertTrue(report.ok)
        self.assertEqual(len(report.known), 1)
        self.assertIn("issue #0 owns the index", render_report(report, base))

    def test_baseline_entry_that_stops_scanning_fails(self):
        conn = fixture_conn()
        conn.execute(DDL)
        base = baseline((FIXTURE_PATH, UNINDEXED_QUERY))
        report = plan(conn, statements(UNINDEXED_QUERY), base)
        self.assertFalse(report.ok)
        self.assertEqual(report.stale, [f"{FIXTURE_PATH} {UNINDEXED_QUERY}"])
        self.assertIn("FAIL: baseline entries with no matching site", render_report(report, base))

    def test_baseline_entry_is_per_file(self):
        base = baseline(("other.py", UNINDEXED_QUERY))
        report = plan(fixture_conn(), statements(UNINDEXED_QUERY), base)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.unacknowledged), 1)
        self.assertEqual(len(report.stale), 1)

    def test_second_site_of_a_known_statement_is_a_new_finding(self):
        base = baseline((FIXTURE_PATH, UNINDEXED_QUERY))
        report = plan(fixture_conn(), statements(UNINDEXED_QUERY, UNINDEXED_QUERY), base)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.known), 1)
        self.assertEqual(len(report.unacknowledged), 1)
        self.assertEqual(report.unacknowledged[0].statement.line, 2)

    def test_an_entry_per_site_covers_both_and_a_lost_site_is_stale(self):
        base = baseline((FIXTURE_PATH, UNINDEXED_QUERY, 2))
        self.assertTrue(plan(fixture_conn(), statements(UNINDEXED_QUERY, UNINDEXED_QUERY), base).ok)
        report = plan(fixture_conn(), statements(UNINDEXED_QUERY), base)
        self.assertFalse(report.ok)
        self.assertIn("(covers 2 site(s), 1 found)", report.stale[0])

    def test_baseline_matches_on_whitespace_normalised_text(self):
        spread = Statement(FIXTURE_PATH, 1, "SELECT id\n    FROM events\n    WHERE b = ?")
        report = plan(fixture_conn(), [spread], baseline((FIXTURE_PATH, UNINDEXED_QUERY)))
        self.assertTrue(report.ok)

    def test_statement_on_a_hermes_table_is_skipped_naming_the_table(self):
        report = plan(fixture_conn(), statements(HERMES_QUERY))
        self.assertTrue(report.ok)
        result = report.results[0]
        self.assertEqual(result.kind, KIND_SKIPPED)
        self.assertEqual(result.message, "sessions")
        self.assertIn("table sessions", render_report(report, {}))

    def test_statement_on_an_unknown_table_is_an_error(self):
        report = plan(fixture_conn(), statements(MISSPELT_QUERY))
        self.assertFalse(report.ok)
        self.assertEqual(report.results[0].kind, KIND_ERROR)
        self.assertIn("no such table: event", render_report(report, {}))

    def test_plan_error_on_an_owned_table_fails(self):
        report = plan(fixture_conn(), statements(BROKEN_QUERY))
        self.assertFalse(report.ok)
        self.assertEqual(report.results[0].kind, KIND_ERROR)
        self.assertIn("no such column: nope", render_report(report, {}))

    def test_ddl_is_listed_not_planned(self):
        report = plan(fixture_conn(), statements(DDL, "PRAGMA table_info(events)", "BEGIN IMMEDIATE"))
        self.assertTrue(report.ok)
        self.assertEqual({r.kind for r in report.results}, {KIND_SCHEMA})

    def test_dynamic_statement_on_an_owned_table_fails_until_baselined(self):
        report = plan(fixture_conn(), statements(DYNAMIC_OWNED))
        self.assertFalse(report.ok)
        self.assertEqual(report.results[0].kind, KIND_DYNAMIC)
        self.assertTrue(report.results[0].on_owned_table)
        self.assertIn("FAIL: dynamic statements on owned tables not in the baseline", render_report(report, {}))
        base = baseline((FIXTURE_PATH, DYNAMIC_OWNED))
        self.assertTrue(plan(fixture_conn(), statements(DYNAMIC_OWNED), base).ok)

    def test_dynamic_statement_on_another_table_is_only_listed(self):
        report = plan(fixture_conn(), statements(DYNAMIC_FOREIGN))
        self.assertTrue(report.ok)
        self.assertEqual(report.results[0].kind, KIND_DYNAMIC)
        self.assertFalse(report.results[0].on_owned_table)
        self.assertIn("dynamic on other tables", render_report(report, {}))

    def test_subquery_scan_is_one_finding_for_the_statement(self):
        nested = "SELECT id FROM events WHERE b = ? OR id IN (SELECT id FROM events WHERE b = ?)"
        report = plan(fixture_conn(), statements(nested))
        self.assertEqual(len(report.unacknowledged), 1)
        self.assertEqual(len(report.unacknowledged[0].scans), 2)

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

    EMBEDDED = textwrap.dedent(
        '''
        PROGRAM = """
        import sqlite3
        conn = sqlite3.connect(path)
        conn.execute("SELECT id FROM events WHERE b = ?")
        """
        NOTE = """
        a note that mentions sqlite3
        and is not a program
        """
        '''
    )

    def setUp(self):
        self.found = collect_statements(FIXTURE_PATH, self.SOURCE)
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

    def test_program_held_in_a_string_is_parsed_for_its_sql(self):
        found = collect_statements(FIXTURE_PATH, self.EMBEDDED)
        self.assertEqual([s.sql for s in found], ["SELECT id FROM events WHERE b = ?"])
        self.assertIn('conn.execute("SELECT id FROM events', self.EMBEDDED.splitlines()[found[0].line - 1])


class DiscoveryTest(unittest.TestCase):
    OWNED = frozenset({"events"})
    CONSUMER = 'import sqlite3\nconn.execute("SELECT id FROM events WHERE b = ?")\n'
    PROSE_ONLY = 'MESSAGE = "the events table is read elsewhere"\n'

    def write(self, root: Path, relative: str, text: str) -> None:
        file = root / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text, encoding="utf-8")

    def test_files_naming_an_owned_table_are_found_and_the_excluded_shapes_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "pkg/consumer.py", self.CONSUMER)
            self.write(root, "pkg/prose.py", self.PROSE_ONLY)
            self.write(root, "pkg/test_consumer.py", self.CONSUMER)
            self.write(root, "patches/verify_consumer.py", self.CONSUMER)
            self.write(root, "bench/plant.py", self.CONSUMER)
            self.write(root, "third_party/vendored.py", self.CONSUMER)
            self.write(root, "roster/server.py", self.CONSUMER)
            found = discover_sources(self.OWNED, root=root, roster=("roster/server.py",))
            self.assertEqual(found, ["pkg/consumer.py"])
            collected = collect_discovered(self.OWNED, found, root=root)
            self.assertEqual([(s.path, s.sql) for s in collected], [("pkg/consumer.py", "SELECT id FROM events WHERE b = ?")])

    def test_only_statements_on_owned_tables_are_collected_from_a_discovered_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "pkg/mixed.py", self.CONSUMER + 'conn.execute("SELECT id FROM tasks WHERE id = ?")\n')
            found = discover_sources(self.OWNED, root=root, roster=())
            self.assertEqual([s.sql for s in collect_discovered(self.OWNED, found, root=root)], ["SELECT id FROM events WHERE b = ?"])


class BaselineParseTest(unittest.TestCase):
    def test_reason_lines_belong_to_the_entry_below_and_a_repeat_adds_a_site(self):
        text = textwrap.dedent(
            """
            # file header

            # first reason
            # second line
            a/b.py SELECT a FROM t

            # other
            c.py DELETE FROM t WHERE x = ?
            c.py DELETE FROM t WHERE x = ?
            """
        )
        parsed = parse_baseline(text)
        self.assertEqual(parsed[("a/b.py", "SELECT a FROM t")], BaselineEntry("first reason second line", 1))
        self.assertEqual(parsed[("c.py", "DELETE FROM t WHERE x = ?")], BaselineEntry("other", 2))

    def test_entry_without_a_reason_is_refused(self):
        with self.assertRaises(ValueError):
            parse_baseline("a.py SELECT a FROM t\n")

    def test_entry_without_a_path_is_refused(self):
        with self.assertRaises(ValueError):
            parse_baseline("# r\nSELECT a FROM t\n")

    def test_missing_file_is_an_empty_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_baseline(Path(tmp) / "absent.txt"), {})


if __name__ == "__main__":
    unittest.main()
