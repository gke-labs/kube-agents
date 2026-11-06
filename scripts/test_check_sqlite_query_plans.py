#!/usr/bin/env python3
"""Tests for scripts/check_sqlite_query_plans.py."""

from __future__ import annotations

import io
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.check_sqlite_query_plans import (  # noqa: E402
    BASELINE,
    EXIT_FAIL,
    EXIT_OK,
    KIND_BOUNDED,
    KIND_DYNAMIC,
    KIND_FINDING,
    KIND_OK,
    KIND_SCHEMA,
    KIND_SKIPPED,
    REPO_ROOT,
    TARGETS,
    SchemaSource,
    check,
    leading_keyword,
    main,
    scan,
    scanned_table,
)

# A module shaped like the server: a database-path hook, an `init_db` that
# builds the schema, an extra DDL constant, and one statement of every shape
# the check distinguishes, each in a function named for what it shows.
FIXTURE_SERVER = textwrap.dedent(
    '''
    import os
    import sqlite3

    import fixture_helper

    DB_PATH = os.environ.get("DB_PATH", "")
    DRIFT_REASON = "drift"
    PENDING = 1
    ALERT_LIMIT = 5
    SIDE_SCHEMA = "CREATE TABLE IF NOT EXISTS side (id TEXT PRIMARY KEY, note TEXT)"
    RECENT_SQL = "SELECT id FROM events WHERE created_at > ?"


    def init_db():
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster TEXT NOT NULL,
                object_uid TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                backlog INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at)")
        conn.execute(f"CREATE INDEX IF NOT EXISTS idx_events_drift ON events (object_uid) WHERE reason = '{DRIFT_REASON}'")
        conn.execute("CREATE TABLE IF NOT EXISTS data_volume_warning (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, author TEXT, body TEXT)")
        for column in ("extra",):
            conn.execute(f"ALTER TABLE notes ADD COLUMN {column} TEXT")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        conn.close()


    def unindexed(conn, cluster):
        return conn.execute("SELECT id FROM events WHERE cluster = ?", (cluster,)).fetchall()


    def indexed(conn):
        return conn.execute(RECENT_SQL, ("2026-01-01",)).fetchall()


    def covering(conn):
        return conn.execute("SELECT COUNT(*) FROM events WHERE created_at >= ?", ("2026-01-01",)).fetchone()


    def partial_literal(conn, uid):
        return conn.execute(f"SELECT id FROM events WHERE reason = '{DRIFT_REASON}' AND object_uid = ?", (uid,)).fetchall()


    def partial_bound(conn, uid):
        return conn.execute("SELECT id FROM events WHERE reason = ? AND object_uid = ?", ("drift", uid)).fetchall()


    def concatenated(conn):
        return conn.execute("SELECT id FROM events " + "WHERE backlog = " + str(PENDING) + " AND created_at > ?", ("x",)).fetchall()


    def local_once(conn):
        columns = "id, cluster"
        return conn.execute(f"SELECT {columns} FROM events WHERE created_at > ?", ("x",)).fetchall()


    def local_twice(conn, flag):
        where = "WHERE cluster = ?"
        if flag:
            where = "WHERE created_at > ?"
        return conn.execute(f"SELECT id FROM events {where}", ("x",)).fetchall()


    def sibling_constant(conn):
        return conn.execute(fixture_helper.HELPER_SQL, ("x",)).fetchall()


    def named_parameters(conn):
        return conn.execute("SELECT id FROM events WHERE created_at > :since", {"since": "x"}).fetchall()


    def hermes(conn, session_id):
        return conn.execute("SELECT id FROM sessions WHERE parent_session_id = ?", (session_id,)).fetchall()


    def dynamic(conn, ids):
        marks = ",".join("?" * len(ids))
        return conn.execute("SELECT id FROM events WHERE id IN ({})".format(marks), ids).fetchall()


    def bounded(conn):
        return conn.execute("SELECT body FROM data_volume_warning").fetchall()


    def catalog(conn):
        return conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()


    def constant_row(conn):
        return conn.execute("SELECT 1").fetchone()


    def side_table(conn, note_id):
        return conn.execute("SELECT note FROM side WHERE id = ?", (note_id,)).fetchone()


    def aliased(conn, author):
        return conn.execute(
            "SELECT a.id FROM notes AS a JOIN events e ON e.id = a.id WHERE a.author = ?", (author,)
        ).fetchall()


    def twice(conn, cluster):
        return conn.execute(
            "SELECT id FROM events WHERE cluster = ? OR id IN (SELECT id FROM events WHERE cluster = ?)",
            (cluster, cluster),
        ).fetchall()


    def many(conn, rows):
        conn.executemany("UPDATE notes SET body = ? WHERE author = ?", rows)


    class Store:
        def method(self, conn):
            return conn.execute("DELETE FROM notes WHERE author = ?", ("x",)).rowcount

        def outer(self, conn):
            def inner():
                return conn.execute("SELECT body FROM notes WHERE author = ?", ("x",)).fetchone()

            return inner()
    '''
)

FIXTURE_HELPER = textwrap.dedent(
    '''
    HELPER_SQL = "SELECT id FROM events WHERE created_at > ?"


    def helper(conn):
        return conn.execute(HELPER_SQL, ("x",)).fetchall()
    '''
)

FIXTURE_SCHEMA = SchemaSource(
    module="fixture_server.py", db_path_attr="DB_PATH", init_function="init_db", extra_ddl_attrs=("SIDE_SCHEMA",)
)
FIXTURE_TARGETS = ("fixture_server.py", "fixture_helper.py")
# What the fixture's findings pin to, exactly.
FIXTURE_BASELINE = textwrap.dedent(
    """
    # the shapes the fixture leaves unindexed on purpose
    fixture_server.py unindexed events 1
    fixture_server.py partial_bound events 1
    fixture_server.py aliased notes 1
    fixture_server.py twice events 2
    fixture_server.py Store.method notes 1
    fixture_server.py Store.outer.inner notes 1
    fixture_server.py many notes 1
    """
)


def _write_fixture(root: Path, baseline: str | None = FIXTURE_BASELINE) -> Path:
    (root / "fixture_server.py").write_text(FIXTURE_SERVER, encoding="utf-8")
    (root / "fixture_helper.py").write_text(FIXTURE_HELPER, encoding="utf-8")
    baseline_path = root / "baseline.txt"
    if baseline is not None:
        baseline_path.write_text(baseline, encoding="utf-8")
    return baseline_path


def _run(root: Path, baseline: Path, list_statements: bool = False, **overrides) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    kwargs = dict(targets=FIXTURE_TARGETS, schema=FIXTURE_SCHEMA, baseline=baseline, list_statements=list_statements)
    kwargs.update(overrides)
    code = check(root, out=out, err=err, **kwargs)
    return code, out.getvalue(), err.getvalue()


def _kinds(listing: str) -> dict[str, list[str]]:
    """function -> the kinds of every verdict listed for it, in order."""
    kinds: dict[str, list[str]] = {}
    for line in listing.splitlines():
        location, rest = line.split(": ", 1)
        function, kind = rest.split(" ", 2)[:2]
        kinds.setdefault(f"{location.split(':')[0]}:{function}", []).append(kind)
    return kinds


class FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_unindexed_query_fails_with_the_plan_line(self) -> None:
        baseline = _write_fixture(self.root, baseline=None)
        code, _, err = _run(self.root, baseline)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("fixture_server.py:", err)
        self.assertIn("unindexed scans events: `SCAN events` for `SELECT id FROM events WHERE cluster = ?`", err)
        self.assertIn("index it or pin it in", err)

    def test_classification_of_every_shape(self) -> None:
        baseline = _write_fixture(self.root)
        code, out, err = _run(self.root, baseline, list_statements=True)
        self.assertEqual(code, EXIT_OK, err)
        kinds = _kinds(out)
        server = "fixture_server.py:"
        # The schema builder's DDL and PRAGMA are listed, never prepared; the
        # ALTER TABLE in a loop does not resolve.
        self.assertEqual(kinds[server + "init_db"], [KIND_SCHEMA] * 5 + [KIND_DYNAMIC, KIND_SCHEMA])
        self.assertEqual(kinds[server + "unindexed"], [KIND_FINDING])
        self.assertEqual(kinds[server + "indexed"], [KIND_OK])
        self.assertEqual(kinds[server + "covering"], [KIND_OK])
        self.assertEqual(kinds[server + "partial_literal"], [KIND_OK])
        # A bound parameter never matches a partial index's WHERE.
        self.assertEqual(kinds[server + "partial_bound"], [KIND_FINDING])
        self.assertEqual(kinds[server + "concatenated"], [KIND_OK])
        self.assertEqual(kinds[server + "local_once"], [KIND_OK])
        self.assertEqual(kinds[server + "local_twice"], [KIND_DYNAMIC])
        self.assertEqual(kinds[server + "sibling_constant"], [KIND_OK])
        self.assertEqual(kinds[server + "named_parameters"], [KIND_OK])
        self.assertEqual(kinds[server + "hermes"], [KIND_SKIPPED])
        self.assertEqual(kinds[server + "dynamic"], [KIND_DYNAMIC])
        self.assertEqual(kinds[server + "bounded"], [KIND_BOUNDED])
        self.assertEqual(kinds[server + "catalog"], [KIND_OK])
        self.assertEqual(kinds[server + "constant_row"], [KIND_OK])
        # The extra DDL constant built the side table, so its primary-key read plans.
        self.assertEqual(kinds[server + "side_table"], [KIND_OK])
        # The planner names the aliased table `a`; the alias maps back to `notes`.
        self.assertEqual(kinds[server + "aliased"], [KIND_FINDING])
        self.assertIn("aliased finding table=notes plan=SCAN a ", out)
        # One statement, two scans of the same table.
        self.assertEqual(kinds[server + "twice"], [KIND_FINDING, KIND_FINDING])
        self.assertEqual(kinds[server + "many"], [KIND_FINDING])
        self.assertEqual(kinds[server + "Store.method"], [KIND_FINDING])
        self.assertEqual(kinds[server + "Store.outer.inner"], [KIND_FINDING])
        self.assertEqual(kinds["fixture_helper.py:helper"], [KIND_OK])
        self.assertIn("hermes skipped table=sessions", out)
        self.assertIn("statement(s) read across 2 file(s)", out)

    def test_concatenated_statement_keeps_the_constant_value(self) -> None:
        baseline = _write_fixture(self.root)
        statements, _, _ = scan(self.root, FIXTURE_TARGETS)
        by_function = {s.function: s.sql for s in statements}
        self.assertEqual(by_function["concatenated"], "SELECT id FROM events WHERE backlog = 1 AND created_at > ?")
        self.assertEqual(by_function["partial_literal"], "SELECT id FROM events WHERE reason = 'drift' AND object_uid = ?")
        self.assertEqual(by_function["local_once"], "SELECT id, cluster FROM events WHERE created_at > ?")
        self.assertEqual(by_function["sibling_constant"], "SELECT id FROM events WHERE created_at > ?")
        self.assertIsNone(by_function["local_twice"])
        self.assertIsNone(by_function["dynamic"])

    def test_prepare_error_fails(self) -> None:
        baseline = _write_fixture(self.root)
        source = FIXTURE_SERVER + textwrap.dedent(
            '''

            def broken(conn):
                return conn.execute("SELECT missing_column FROM events WHERE id = ?", (1,)).fetchone()
            '''
        )
        (self.root / "fixture_server.py").write_text(source, encoding="utf-8")
        code, _, err = _run(self.root, baseline)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("broken: `SELECT missing_column FROM events WHERE id = ?` does not prepare: no such column", err)

    def test_baseline_rise_drop_stale_unscanned_malformed_duplicate(self) -> None:
        baseline = _write_fixture(
            self.root,
            baseline=textwrap.dedent(
                """
                fixture_server.py unindexed events 1
                fixture_server.py partial_bound events 1
                fixture_server.py aliased notes 1
                fixture_server.py twice events 1
                fixture_server.py Store.method notes 2
                fixture_server.py Store.outer.inner notes 1
                fixture_server.py indexed events 1
                other.py unindexed events 1
                fixture_server.py unindexed events
                fixture_server.py unindexed events 0
                fixture_server.py unindexed events 1
                """
            ),
        )
        code, _, err = _run(self.root, baseline)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("twice scans events 2 time(s), the entry at baseline.txt:5 allows 1; index the new one", err)
        self.assertIn("Store.method scans notes 1 time(s), the entry at baseline.txt:6 allows 2; lower the entry to 1", err)
        self.assertIn("baseline.txt:8: fixture_server.py indexed scans events no more; remove the entry", err)
        self.assertIn("baseline.txt:9: other.py is not a file this check reads; remove the entry", err)
        self.assertIn("baseline.txt:10: expected `<file> <function> <table> <count>`", err)
        self.assertIn("baseline.txt:11: expected `<file> <function> <table> <count>`", err)
        self.assertIn("baseline.txt:12: fixture_server.py unindexed events already has an entry at line 2", err)
        # `many` has no entry and is reported as a finding beside the problems.
        self.assertIn("many scans notes: `SCAN notes`", err)
        self.assertIn("1 finding(s) not pinned and 7 problem(s)", err)

    def test_missing_target_fails(self) -> None:
        baseline = _write_fixture(self.root)
        code, _, err = _run(self.root, baseline, targets=FIXTURE_TARGETS + ("gone.py",))
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("gone.py: a configured target this check reads is missing", err)

    def test_schema_module_that_does_not_import_fails(self) -> None:
        baseline = _write_fixture(self.root)
        (self.root / "fixture_server.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        code, _, err = _run(self.root, baseline)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("fixture_server.py: the schema module does not import: RuntimeError: boom", err)

    def test_schema_module_missing_an_attribute_fails(self) -> None:
        baseline = _write_fixture(self.root)
        schema = SchemaSource(module="fixture_server.py", db_path_attr="DB_PATH", init_function="no_such_init")
        code, _, err = _run(self.root, baseline, schema=schema)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("fixture_server.py: the schema module has no attribute no_such_init", err)


class PlanLineTests(unittest.TestCase):
    def test_scan_lines(self) -> None:
        self.assertEqual(scanned_table("SCAN events"), "events")
        self.assertEqual(scanned_table("SCAN events AS e"), "events")
        self.assertEqual(scanned_table("SCAN TABLE events"), "events")
        self.assertEqual(scanned_table("SCAN events USING AUTOMATIC COVERING INDEX (cluster=?)"), "events")
        self.assertIsNone(scanned_table("SCAN events USING INDEX idx_events_created_at"))
        self.assertIsNone(scanned_table("SCAN events USING COVERING INDEX idx_events_created_at"))
        self.assertIsNone(scanned_table("SEARCH events USING INTEGER PRIMARY KEY (rowid=?)"))
        self.assertIsNone(scanned_table("LIST SUBQUERY 1"))
        # A constant row and a subquery are names the schema does not hold, which judge() drops.
        self.assertEqual(scanned_table("SCAN CONSTANT ROW"), "CONSTANT")
        self.assertEqual(scanned_table("SCAN (subquery-1)"), "(subquery-1)")

    def test_leading_keyword(self) -> None:
        self.assertEqual(leading_keyword("\n  -- a note\n  select 1"), "SELECT")
        self.assertEqual(leading_keyword("PRAGMA journal_mode=WAL"), "PRAGMA")
        self.assertEqual(leading_keyword("BEGIN IMMEDIATE"), "BEGIN")
        self.assertEqual(leading_keyword("WITH c AS (SELECT 1) SELECT * FROM c"), "WITH")
        self.assertEqual(leading_keyword(""), "")


class CheckoutTests(unittest.TestCase):
    def test_checkout_passes_with_its_baseline_under_thirty_seconds(self) -> None:
        for target in TARGETS:
            self.assertTrue((REPO_ROOT / target).is_file(), target)
        self.assertTrue(BASELINE.is_file())
        out, err = io.StringIO(), io.StringIO()
        started = time.monotonic()
        code = check(out=out, err=err)
        elapsed = time.monotonic() - started
        self.assertEqual(code, EXIT_OK, err.getvalue())
        self.assertLess(elapsed, 30.0)
        self.assertIn("check_sqlite_query_plans:", out.getvalue())
        self.assertIn("skipped on tables the repository does not define", out.getvalue())

    def test_main_lists_the_checkout(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(main(["--list"], out=out, err=err), EXIT_OK, err.getvalue())
        listing = out.getvalue()
        self.assertIn("agents/platform/scripts/session_kv_server.py:", listing)
        self.assertIn(" skipped table=sessions ", listing)


if __name__ == "__main__":
    unittest.main()
