#!/usr/bin/env python3
"""Tests for scripts/check_sqlite_query_plans.py."""

from __future__ import annotations

import io
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

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
    explain,
    leading_keyword,
    main,
    cte_names,
    derived_names,
    partial_indexes,
    planned_text,
    scan,
    scan_index,
    scanned_table,
    table_aliases,
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
    COLUMNS = ("id", "cluster", "reason")
    SELECT_COLUMNS = f"SELECT {', '.join(COLUMNS)} FROM events"
    shadowed = "SELECT id FROM events WHERE id = ?"
    FALLBACK_SQL = "SELECT id FROM events WHERE id = ?"
    try:
        import fixture_absent_module  # noqa: F401
    except ImportError:
        FALLBACK_SQL = "SELECT id FROM events WHERE cluster = ?"
    GLOBAL_SQL = "SELECT id FROM events WHERE id = ?"
    # The helper module has a HELPER_TABLE of its own; its constants resolve against that one, not this.
    HELPER_TABLE = "notes"


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


    def joined_columns(conn):
        return conn.execute(f"{SELECT_COLUMNS} WHERE created_at > ?", ("x",)).fetchall()


    def joined_comprehension(conn):
        return conn.execute(f"SELECT {', '.join(c for c in COLUMNS)} FROM events WHERE id = ?", (1,)).fetchall()


    def joined_list(conn):
        assignments = []
        assignments.append("cluster = ?")
        return conn.execute(f"UPDATE events SET {', '.join(assignments)} WHERE id = ?", ("x", 1)).rowcount


    def unpacked_shadow(conn):
        shadowed, params = "SELECT id FROM events WHERE cluster = ?", ("x",)
        return conn.execute(shadowed, params).fetchall()


    def except_shadow(conn):
        try:
            return conn.execute(shadowed, (1,)).fetchall()
        except ValueError as shadowed:
            raise RuntimeError(str(shadowed))


    def closure(conn):
        shadowed = "SELECT id FROM events WHERE cluster = ?"

        def inner():
            return conn.execute(shadowed, ("x",)).fetchall()

        return inner()


    def closure_inner_wins(conn):
        columns = "cluster"

        def inner():
            columns = "id"
            return conn.execute(f"SELECT {columns} FROM events WHERE created_at > ?", ("x",)).fetchall()

        return inner()


    def closure_own_name(conn):
        columns = "id"
        sql = f"SELECT {columns} FROM events WHERE created_at > ?"

        def inner():
            columns = "cluster"
            return conn.execute(sql, ("x",)).fetchall()

        return inner()


    def closure_nonlocal(conn):
        columns = "id"
        sql = f"SELECT {columns} FROM events WHERE created_at > ?"

        def inner():
            nonlocal columns
            columns = "cluster"
            return conn.execute(sql, ("x",)).fetchall()

        return inner()


    def nested_helper_name(conn):
        sql = "SELECT id FROM events WHERE cluster = ?"

        def fmt(sql):
            return sql.strip()

        return conn.execute(sql, (fmt("x"),)).fetchall()


    def module_rebound_in_block(conn):
        return conn.execute(FALLBACK_SQL, (1,)).fetchall()


    def rebind_global():
        global GLOBAL_SQL
        GLOBAL_SQL = "SELECT id FROM events WHERE cluster = ?"


    def global_rebound(conn):
        return conn.execute(GLOBAL_SQL, (1,)).fetchall()


    def sibling_built_constant(conn):
        return conn.execute(fixture_helper.HELPER_SCAN_SQL, ("x",)).fetchall()


    def index_walk(conn):
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()


    def partial_index_walk(conn):
        return conn.execute("SELECT id FROM events WHERE reason = 'drift'").fetchall()


    def aliased_subquery(conn):
        return conn.execute(
            "SELECT g.cluster FROM (SELECT cluster, COUNT(*) AS c FROM events WHERE created_at > ? GROUP BY cluster) AS g "
            "WHERE g.c > ?",
            ("x", 1),
        ).fetchall()


    def aliased_cte(conn):
        return conn.execute(
            "WITH recent AS (SELECT cluster, COUNT(*) AS c FROM events WHERE created_at > ? GROUP BY cluster) "
            "SELECT r.cluster FROM recent r WHERE r.c > ?",
            ("x", 1),
        ).fetchall()


    def materialized_subquery(conn):
        return conn.execute(
            "SELECT e.id FROM events e JOIN (SELECT author FROM notes GROUP BY author) a ON a.author = e.cluster "
            "WHERE e.created_at > ?",
            ("x",),
        ).fetchall()


    def automatic_index(conn):
        return conn.execute(
            "SELECT n.body FROM events e JOIN notes n ON n.author = e.cluster WHERE e.created_at > ?", ("x",)
        ).fetchall()


    def create_as_select(conn, cluster):
        conn.execute("CREATE TEMP TABLE recent AS SELECT id FROM events WHERE cluster = ?", (cluster,))


    def create_view(conn):
        conn.execute("CREATE VIEW IF NOT EXISTS recent_events AS SELECT id FROM events WHERE created_at > '2026'")


    def comma_list(conn, cluster):
        return conn.execute(
            "SELECT e.id FROM notes n, events e WHERE e.cluster = ? AND n.id = e.id", (cluster,)
        ).fetchall()


    def quoted_alias(conn, cluster):
        return conn.execute('SELECT "e".id FROM events AS "e" WHERE "e".cluster = ?', (cluster,)).fetchall()


    def cte(conn, cluster):
        return conn.execute(
            "WITH RECURSIVE chain(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM chain WHERE n < 3) "
            "SELECT n FROM chain",
        ).fetchall()


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
    HELPER_TABLE = "events"
    HELPER_SCAN_SQL = f"SELECT id FROM {HELPER_TABLE} WHERE cluster = ?"


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
    fixture_server.py unpacked_shadow events 1
    fixture_server.py closure.inner events 1
    fixture_server.py nested_helper_name events 1
    fixture_server.py sibling_built_constant events 1
    fixture_server.py index_walk events 1
    fixture_server.py materialized_subquery notes 1
    fixture_server.py automatic_index notes 1
    fixture_server.py create_as_select events 1
    fixture_server.py comma_list events 1
    fixture_server.py quoted_alias events 1
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
        # `", ".join(COLUMNS)` over a tuple constant resolves; over a comprehension it does not.
        self.assertEqual(kinds[server + "joined_columns"], [KIND_OK])
        self.assertEqual(kinds[server + "joined_comprehension"], [KIND_DYNAMIC])
        # A list literal can be appended to after it is bound, so a join over it does not resolve.
        self.assertEqual(kinds[server + "joined_list"], [KIND_DYNAMIC])
        # A local bound by unpacking is the statement judged, not the module constant it shadows.
        self.assertEqual(kinds[server + "unpacked_shadow"], [KIND_FINDING])
        # A name also bound by `except ... as` resolves to nothing, not to the module constant.
        self.assertEqual(kinds[server + "except_shadow"], [KIND_DYNAMIC])
        # A nested function reads the enclosing function's local, not the module constant it shadows.
        self.assertEqual(kinds[server + "closure.inner"], [KIND_FINDING])
        self.assertEqual(kinds[server + "closure_inner_wins.inner"], [KIND_OK])
        # A name the nested function binds is its own variable: the outer statement keeps the outer value.
        self.assertEqual(kinds[server + "closure_own_name.inner"], [KIND_OK])
        # A `nonlocal` rebinding in the nested function is a second binding of the outer name, so nothing resolves.
        self.assertEqual(kinds[server + "closure_nonlocal.inner"], [KIND_DYNAMIC])
        # A nested helper's parameter of the same name does not count against the enclosing function's local.
        self.assertEqual(kinds[server + "nested_helper_name"], [KIND_FINDING])
        # A module constant rebound inside a top-level `try`, or through `global` in a function, is bound twice.
        self.assertEqual(kinds[server + "module_rebound_in_block"], [KIND_DYNAMIC])
        self.assertEqual(kinds[server + "global_rebound"], [KIND_DYNAMIC])
        # A sibling's constant resolves against the sibling's names: its table, not this module's HELPER_TABLE.
        self.assertEqual(kinds[server + "sibling_built_constant"], [KIND_FINDING])
        self.assertIn("sibling_built_constant finding table=events plan=SCAN events", out)
        # A SCAN through an index still reads every row; through a partial index it reads what that index admits.
        self.assertEqual(kinds[server + "index_walk"], [KIND_FINDING])
        self.assertIn("index_walk finding table=events plan=SCAN events USING COVERING INDEX idx_events_created_at", out)
        self.assertEqual(kinds[server + "partial_index_walk"], [KIND_BOUNDED])
        self.assertIn("partial_index_walk bounded table=events plan=SCAN events USING ", out)
        self.assertIn("INDEX idx_events_drift", out)
        # A derived table the plan declares (CO-ROUTINE or MATERIALIZE) is no table under its alias either.
        self.assertEqual(kinds[server + "aliased_subquery"], [KIND_OK])
        self.assertEqual(kinds[server + "aliased_cte"], [KIND_OK])
        self.assertEqual(kinds[server + "materialized_subquery"], [KIND_FINDING])
        self.assertIn("materialized_subquery finding table=notes plan=SCAN notes", out)
        # The planner builds an automatic index by reading the whole inner table; its line says SEARCH.
        self.assertEqual(kinds[server + "automatic_index"], [KIND_FINDING])
        self.assertIn("automatic_index finding table=notes plan=SEARCH n USING AUTOMATIC COVERING INDEX (author=?)", out)
        # The query under a CREATE ... AS SELECT is planned; a view's indexed query passes.
        self.assertEqual(kinds[server + "create_as_select"], [KIND_FINDING])
        self.assertIn("create_as_select finding table=events plan=SCAN events", out)
        self.assertEqual(kinds[server + "create_view"], [KIND_OK])
        # The second table of a comma list, and a double-quoted alias, map back to their tables.
        self.assertEqual(kinds[server + "comma_list"], [KIND_FINDING])
        self.assertIn("comma_list finding table=events plan=SCAN e ", out)
        self.assertEqual(kinds[server + "quoted_alias"], [KIND_FINDING])
        # A recursive CTE scans its own rows, which is no table.
        self.assertEqual(kinds[server + "cte"], [KIND_OK])
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
        self.assertEqual(by_function["joined_columns"], "SELECT id, cluster, reason FROM events WHERE created_at > ?")
        self.assertEqual(by_function["unpacked_shadow"], "SELECT id FROM events WHERE cluster = ?")
        self.assertEqual(by_function["closure.inner"], "SELECT id FROM events WHERE cluster = ?")
        # The inner binding wins; a name the nested function binds is its own, and a `nonlocal` rebind is the outer's.
        self.assertEqual(by_function["closure_inner_wins.inner"], "SELECT id FROM events WHERE created_at > ?")
        self.assertEqual(by_function["closure_own_name.inner"], "SELECT id FROM events WHERE created_at > ?")
        self.assertIsNone(by_function["closure_nonlocal.inner"])
        self.assertEqual(by_function["nested_helper_name"], "SELECT id FROM events WHERE cluster = ?")
        self.assertIsNone(by_function["module_rebound_in_block"])
        self.assertIsNone(by_function["global_rebound"])
        self.assertEqual(by_function["sibling_built_constant"], "SELECT id FROM events WHERE cluster = ?")
        self.assertIsNone(by_function["except_shadow"])
        self.assertIsNone(by_function["joined_comprehension"])
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

    def test_unmapped_scan_name_is_a_problem_not_a_pass(self) -> None:
        baseline = _write_fixture(self.root)
        source = FIXTURE_SERVER + textwrap.dedent(
            '''

            def odd_alias(conn, cluster):
                return conn.execute("SELECT x.id FROM main.events x WHERE x.cluster = ?", (cluster,)).fetchall()
            '''
        )
        (self.root / "fixture_server.py").write_text(source, encoding="utf-8")
        code, out, err = _run(self.root, baseline, list_statements=True)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("odd_alias: `SCAN x` names 'x', which is neither a table the schema defines nor an alias", err)
        self.assertIn("odd_alias error table=- plan=SCAN x", out)
        self.assertNotIn("odd_alias ok", out)

    def test_rebound_alias_is_a_problem_not_a_pass(self) -> None:
        baseline = _write_fixture(self.root)
        source = FIXTURE_SERVER + textwrap.dedent(
            '''

            def rebound_alias(conn, author):
                return conn.execute(
                    "SELECT id FROM events e WHERE EXISTS (SELECT 1 FROM notes e WHERE e.author = ?)", (author,)
                ).fetchall()
            '''
        )
        (self.root / "fixture_server.py").write_text(source, encoding="utf-8")
        code, out, err = _run(self.root, baseline, list_statements=True)
        self.assertEqual(code, EXIT_FAIL)
        # Newer SQLite prints `SCAN e EXISTS` for this shape, so the plan text is not pinned.
        self.assertIn("rebound_alias: `SCAN e", err)
        self.assertIn("names 'e', an alias the statement binds to more than one table; give each reference its own alias", err)
        self.assertNotIn("rebound_alias ok", out)
        self.assertNotIn("rebound_alias finding", out)

    def test_old_sqlite_is_a_problem_not_a_pass(self) -> None:
        baseline = _write_fixture(self.root)
        with mock.patch.object(sqlite3, "sqlite_version_info", (3, 34, 1)), mock.patch.object(sqlite3, "sqlite_version", "3.34.1"):
            code, out, err = _run(self.root, baseline, list_statements=True)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("sqlite3 library 3.34.1 is older than 3.36.0", err)
        self.assertEqual(out, "")

    def test_explain_binds_null_to_named_and_positional_parameters(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT)")
        self.assertEqual(explain(conn, "SELECT a FROM t WHERE id = :id AND a = :a"), ["SEARCH t USING INTEGER PRIMARY KEY (rowid=?)"])
        self.assertEqual(explain(conn, "SELECT a FROM t WHERE id = ? AND a = ?"), ["SEARCH t USING INTEGER PRIMARY KEY (rowid=?)"])
        self.assertEqual(explain(conn, "SELECT a FROM t"), ["SCAN t"])
        with self.assertRaises(sqlite3.ProgrammingError):
            explain(conn, "SELECT 1; SELECT 2")

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
        # `many`, `comma_list`, `quoted_alias`, `unpacked_shadow`, `closure.inner`, `nested_helper_name`,
        # `sibling_built_constant`, `index_walk`, `materialized_subquery`, `automatic_index` and `create_as_select`
        # have no entry and are reported beside the problems.
        self.assertIn("many scans notes: `SCAN notes`", err)
        self.assertIn("automatic_index scans notes: `SEARCH n USING AUTOMATIC COVERING INDEX (author=?)`", err)
        self.assertIn("index_walk scans events: `SCAN events USING COVERING INDEX idx_events_created_at`", err)
        self.assertIn("11 finding(s) not pinned and 7 problem(s)", err)

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
        # An automatic index is a full read of the table under a SEARCH verb.
        self.assertEqual(scanned_table("SEARCH notes USING AUTOMATIC COVERING INDEX (author=?)"), "notes")
        self.assertEqual(scanned_table("SEARCH notes USING AUTOMATIC PARTIAL COVERING INDEX (author=?)"), "notes")
        self.assertEqual(scanned_table("SEARCH notes AS n USING AUTOMATIC INDEX (author=?)"), "notes")
        # A SCAN through an index walks the whole index: every row, the LIMIT (if any) not in the line.
        self.assertEqual(scanned_table("SCAN events USING INDEX idx_events_created_at"), "events")
        self.assertEqual(scanned_table("SCAN events USING COVERING INDEX idx_events_created_at"), "events")
        self.assertEqual(scan_index("SCAN events USING INDEX idx_events_created_at"), "idx_events_created_at")
        self.assertEqual(scan_index("SCAN events USING COVERING INDEX sqlite_autoindex_events_1"), "sqlite_autoindex_events_1")
        self.assertIsNone(scan_index("SCAN events"))
        self.assertIsNone(scan_index("SEARCH events USING INDEX idx_events_created_at (created_at>?)"))
        self.assertIsNone(scan_index("SEARCH notes USING AUTOMATIC COVERING INDEX (author=?)"))
        self.assertIsNone(scanned_table("SEARCH events USING INTEGER PRIMARY KEY (rowid=?)"))
        self.assertIsNone(scanned_table("SEARCH events USING INDEX idx_events_created_at (created_at>?)"))
        self.assertIsNone(scanned_table("LIST SUBQUERY 1"))
        # A constant row and a subquery are names the schema does not hold, which judge() drops.
        self.assertEqual(scanned_table("SCAN CONSTANT ROW"), "CONSTANT")
        self.assertEqual(scanned_table("SCAN (subquery-1)"), "(subquery-1)")

    def test_aliases_and_ctes(self) -> None:
        tables = frozenset({"events", "notes"})
        self.assertEqual(table_aliases("SELECT e.id FROM events e JOIN notes AS n ON n.id = e.id", tables), {"e": "events", "n": "notes"})
        self.assertEqual(table_aliases("SELECT * FROM notes n, events e WHERE e.id = n.id", tables), {"n": "notes", "e": "events"})
        self.assertEqual(table_aliases('UPDATE OR REPLACE events AS "e" SET cluster = ? WHERE "e".id = ?', tables), {"e": "events"})
        self.assertEqual(table_aliases("SELECT id FROM events WHERE id = ? ORDER BY id DESC LIMIT ?", tables), {})
        self.assertEqual(table_aliases("INSERT INTO events (id, cluster) VALUES (?, ?)", tables), {})
        self.assertEqual(table_aliases("SELECT id FROM other o", tables), {})
        # One alias bound to the same table twice maps; bound to two tables, it maps to nothing.
        self.assertEqual(table_aliases("SELECT id FROM events r WHERE id IN (SELECT id FROM events r)", tables), {"r": "events"})
        self.assertEqual(table_aliases("SELECT id FROM events e WHERE EXISTS (SELECT 1 FROM notes e)", tables), {"e": None})
        self.assertEqual(cte_names("WITH RECURSIVE c(n) AS (SELECT 1), d AS (SELECT 2) SELECT * FROM c, d"), frozenset({"c", "d"}))
        self.assertEqual(cte_names("SELECT id FROM events WHERE id = ?"), frozenset())
        # An alias of a CTE or a derived table maps to that name once it is among the names offered.
        self.assertEqual(table_aliases("WITH recent AS (SELECT 1) SELECT r.x FROM recent r", tables | {"recent"}), {"r": "recent"})
        self.assertEqual(
            derived_names(["CO-ROUTINE g", "SCAN events", "USE TEMP B-TREE FOR GROUP BY", "SCAN g", "MATERIALIZE a", "SCAN a"]),
            frozenset({"g", "a"}),
        )
        self.assertEqual(derived_names(["SEARCH events USING INDEX idx (created_at>?)"]), frozenset())

    def test_partial_indexes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b INTEGER)")
        conn.execute("CREATE INDEX idx_t_a ON t (a)")
        conn.execute("CREATE INDEX idx_t_pending ON t (a) WHERE b = 1")
        conn.execute("CREATE TABLE u (k TEXT PRIMARY KEY, v TEXT)")
        self.assertEqual(partial_indexes(conn, frozenset({"t", "u"})), frozenset({"idx_t_pending"}))
        # The partial index serves the scan (3.53 prints COVERING where 3.40 does not, so the line is read, not
        # pinned); a primary key's automatic index is not partial, and a projection of it walks the whole index.
        (plan,) = explain(conn, "SELECT a FROM t WHERE b = 1")
        self.assertEqual((scanned_table(plan), scan_index(plan)), ("t", "idx_t_pending"))
        self.assertEqual(explain(conn, "SELECT k FROM u"), ["SCAN u USING COVERING INDEX sqlite_autoindex_u_1"])

    def test_planned_text(self) -> None:
        self.assertEqual(planned_text("SELECT id FROM events WHERE id = ?"), "SELECT id FROM events WHERE id = ?")
        self.assertEqual(planned_text("CREATE TEMP TABLE r AS SELECT id FROM events"), "SELECT id FROM events")
        self.assertEqual(planned_text("CREATE VIEW v AS\n  WITH c AS (SELECT 1) SELECT * FROM c"), "WITH c AS (SELECT 1) SELECT * FROM c")
        self.assertEqual(planned_text("create table if not exists r as select id from events"), "select id from events")
        self.assertIsNone(planned_text("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, created_at TEXT)"))
        self.assertIsNone(planned_text("CREATE INDEX IF NOT EXISTS idx ON events (created_at)"))
        self.assertIsNone(planned_text("PRAGMA journal_mode=WAL"))
        self.assertIsNone(planned_text("BEGIN IMMEDIATE"))

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
