#!/usr/bin/env python3
"""Fail when a fixed SQL statement in the session server or its helpers would scan a whole table.

The session KV server's ledger grows with the fleet: every intercepted event,
incident, session and finding is a row in `session_kv.db`. A statement that
reads one of those tables without an index reads all of it, and nothing in the
unit tests sees that, because every test runs against a handful of rows. Two
such statements shipped that way (the `intercepted_events` fold lookup, #4331,
and the `session_metadata` retention delete, #3567). This check asks SQLite's
own planner instead, offline, and fails a pull request that adds another.

`TARGETS` are read by AST. A statement is the first argument of any
`.execute(...)` or `.executemany(...)` call that resolves without running the
module: a string literal, a module-level constant, an f-string or `+`
concatenation of those with `str(...)` of one or `", ".join(...)` over a tuple
of them allowed (so a partial index's literal and a column list keep their
values; a list can be appended to after its literal, so it does not count), a constant of another target module named through it
(`findings_queue.X`), or a name bound exactly once in the enclosing function,
by a plain assignment (`a = x`, or `a, b = x, y` element-wise), to such an
expression (a name bound any other way, or in two places, resolves to
nothing, never to a module constant of the same name). Anything else (`.format(...)`, `%`, a join over a comprehension, a name
assigned in a branch) is listed as `dynamic` and not judged.

The schema is the real one, not a replay of the DDL: `SCHEMA_SOURCE` names the
server module, the attribute that holds its database path, the function that
builds the schema and the extra DDL constants to run after it. The module is
imported with that path pointed at a temporary file, as its own test does,
`init_db()` builds every table plus the columns its migration loops add, and the
kanban origins side table is created from its constant. A statement whose
leading keyword is DDL, a transaction word or `PRAGMA` is listed as `schema`
and never prepared (some of those take effect at prepare time).

Every other statement is prepared under `EXPLAIN QUERY PLAN` with a NULL bound
to each parameter. `no such table` means a table this repository does not
define (Hermes's `state.db` and `kanban.db` tables); the statement is listed as
`skipped` with the table named, and not judged. Any other prepare error (a
missing column, a syntax error) is a problem that fails the check. A plan line
`SCAN <table>` on a table the schema defines, without `USING INDEX` or
`USING COVERING INDEX` (an `AUTOMATIC` index is built by scanning the table
and does not count; the planner names an aliased table by its alias, which
the check maps back from the statement's FROM, JOIN, UPDATE, INTO and comma
list words), is a finding, unless the table is in `BOUNDED_TABLES`:
tables that hold one row, or a daily-pruned or capped handful, by design. A
scan of `sqlite_master`, of a constant row, of a subquery or of a CTE the
statement declares is not a table scan; a scan of any other name the
statement does not declare is a problem that fails the check, so an alias
shape the check does not read cannot pass as clean.

``scripts/sqlite_query_plan_baseline.txt``: one line per (file, function,
table) that carried a full scan when the check arrived,
`<file> <function> <table> <count>`, with the owner of the fix in a comment
above it. The list only shrinks: a count above the entry fails (a rise), a
count below fails until the entry is lowered or removed (a drop), an entry
whose function scans the table no more fails (stale), an entry naming a file
the check does not read fails, and a line that is not an entry fails. A site
with no entry is held at zero. A configured target that is missing is a
failure too, so a moved file cannot turn the check green by leaving the scan.

Exit 0 when every finding is pinned exactly; 1 otherwise. `--list` prints every
statement the scan saw with its classification, pinned or not.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

REPO_ROOT = Path(__file__).resolve().parent.parent

# The Python modules the check reads, repository-relative: the session KV
# server, the findings queue it folds into its database, and the two state.db
# helpers.
TARGETS: tuple[str, ...] = (
    "agents/platform/scripts/session_kv_server.py",
    "agents/platform/scripts/findings_queue.py",
    "agents/platform/scripts/session_retention.py",
    "agents/platform/scripts/session_manager.py",
)


@dataclass(frozen=True)
class SchemaSource:
    """Where the schema comes from: a module, its database-path hook, and what builds the tables."""

    # The module that owns the schema, repository-relative.
    module: str
    # The module attribute (and environment variable of the same name) that
    # holds the database path; both are pointed at a temporary file.
    db_path_attr: str
    # The module function that creates the tables, called with no arguments.
    init_function: str
    # Module attributes holding DDL to run after it, one statement each.
    extra_ddl_attrs: tuple[str, ...] = ()


SCHEMA_SOURCE = SchemaSource(
    module="agents/platform/scripts/session_kv_server.py",
    db_path_attr="SESSION_KV_DB_PATH",
    init_function="init_db",
    extra_ddl_attrs=("KANBAN_TASK_ORIGINS_SCHEMA",),
)
SCHEMA_DB_FILENAME = "schema.db"

# Tables the schema defines whose row count is bounded by design, so a scan of
# one reads a handful of rows: `data_volume_warning` and `drift_backlog_lease`
# hold one row, `alert_quota` is pruned daily, `triage_pr_reservations` is
# capped by the open-triage-pull-request limit.
BOUNDED_TABLES = frozenset({"alert_quota", "data_volume_warning", "drift_backlog_lease", "triage_pr_reservations"})
# SQLite's own schema table, under both of its names; a scan of it is a catalog
# read, not a ledger read.
SCHEMA_TABLES = frozenset({"sqlite_master", "sqlite_schema"})

# The shrinking baseline: `<file> <function> <table> <count>` per line, `#`
# comments and blank lines ignored.
BASELINE = REPO_ROOT / "scripts" / "sqlite_query_plan_baseline.txt"
BASELINE_FIELDS = 4
COMMENT_PREFIX = "#"

# The calls whose first argument is a statement.
EXECUTE_METHODS = frozenset({"execute", "executemany"})
# Statements that are schema, transaction or pragma work by their leading
# keyword: listed, never prepared.
SCHEMA_KEYWORDS = frozenset(
    {
        "CREATE",
        "ALTER",
        "DROP",
        "PRAGMA",
        "BEGIN",
        "COMMIT",
        "END",
        "ROLLBACK",
        "SAVEPOINT",
        "RELEASE",
        "VACUUM",
        "ANALYZE",
        "REINDEX",
        "ATTACH",
        "DETACH",
    }
)
SQL_LINE_COMMENT = "--"
EXPLAIN_PREFIX = "EXPLAIN QUERY PLAN "
# How sqlite3 reports the parameter count of a statement prepared with none.
BINDINGS_RE = re.compile(r"The current statement uses (\d+)")
NO_SUCH_TABLE_PREFIX = "no such table: "
# A plan line that reads a whole table: `SCAN <name>` (`SCAN TABLE <name>` on
# SQLite before 3.36), the name possibly followed by `AS <alias>`; then
# whatever the scan uses.
SCAN_RE = re.compile(r"^SCAN (?:TABLE )?(?P<name>\S+)(?: AS \S+)?(?P<rest>.*)$")
INDEXED_SCAN_MARKERS = (" USING INDEX ", " USING COVERING INDEX ")
# The planner names a table by its alias when the statement gives one
# (`FROM intercepted_events e` plans as `SCAN e`), so the aliases are read
# from the statement: the word after FROM, JOIN, UPDATE (with an optional
# `OR <conflict>`), INTO or a comma (a FROM list), optionally double-quoted,
# then an optional `AS` and the alias, unless the next word is a clause
# keyword. A comma also follows a column in a SELECT list or an ORDER BY, so
# a column named like a table may map a keyword as its alias; the planner
# never prints a keyword as a scan's name, so such an entry is inert.
SQL_CLAUSE_KEYWORDS = (
    "AND|AS|ASC|BY|COLLATE|CROSS|DEFAULT|DESC|ELSE|END|EXCEPT|FROM|GROUP|HAVING|IN|INDEXED|INNER|INTERSECT|IS|JOIN|"
    "LEFT|LIMIT|NATURAL|NOT|ON|OR|ORDER|OUTER|RETURNING|RIGHT|SELECT|SET|THEN|UNION|USING|VALUES|WHEN|WHERE|WINDOW"
)
ALIAS_RE = re.compile(
    r"(?:\b(?:FROM|JOIN|INTO|UPDATE(?:\s+OR\s+\w+)?)|,)\s*\"?(?P<table>\w+)\"?"
    r"(?:\s+(?:AS\s+)?(?!(?:" + SQL_CLAUSE_KEYWORDS + r")\b)\"?(?P<alias>\w+)\"?)?",
    re.IGNORECASE,
)
# A common table expression: `WITH [RECURSIVE] name [(columns)] AS (`, and
# `, name [(columns)] AS (` for the next one. The planner names a CTE's rows
# by that name, which is no table.
CTE_RE = re.compile(r"\b(?P<name>\w+)(?:\s*\([\w\s,]*\))?\s+AS\s*\(", re.IGNORECASE)
# Scan names that are no table and no alias: `SCAN CONSTANT ROW`, `SCAN 2
# CONSTANT ROWS`, and a subquery the planner did not flatten.
NON_TABLE_SCAN_NAMES = frozenset({"CONSTANT"})
SUBQUERY_SCAN_PREFIXES = ("(", "SUBQUERY")
# The two calls a statement may be built with: `str(CONSTANT)` in a
# concatenation, and `", ".join(COLUMNS)` over a tuple of constants.
STR_CALL = "str"
JOIN_METHOD = "join"
# The detail column of an EXPLAIN QUERY PLAN row.
PLAN_DETAIL_COLUMN = 3
# How a function is named: `Class.method`, `outer.inner`, and this for a
# statement outside any function.
QUALIFIED_SEPARATOR = "."
MODULE_LEVEL = "(module)"
# A statement built from constants that point at each other deeper than this is
# not something the check reads.
RESOLVE_DEPTH = 8

# Classifications, as `--list` prints them.
KIND_OK = "ok"
KIND_FINDING = "finding"
KIND_BOUNDED = "bounded"
KIND_SKIPPED = "skipped"
KIND_DYNAMIC = "dynamic"
KIND_SCHEMA = "schema"
KIND_ERROR = "error"

# How the report reads.
FINDING_LINE = "{file}:{line}: {function} scans {table}: `{plan}` for `{statement}`; index it or pin it in {baseline}"
LIST_LINE = "{file}:{line}: {function} {kind} table={table} plan={plan} statement={statement}"
PREPARE_ERROR = "{file}:{line}: {function}: `{statement}` does not prepare: {error}"
PLAN_NAME_ERROR = (
    "{file}:{line}: {function}: `{plan}` names {name!r}, which is neither a table the schema defines nor an alias or "
    "CTE the statement declares; teach {script} the shape"
)
TARGET_MISSING_ERROR = "{target}: a configured target this check reads is missing; move the entry in {script} with the file"
SCHEMA_IMPORT_ERROR = "{module}: the schema module does not import: {error}"
SCHEMA_ATTR_ERROR = "{module}: the schema module has no attribute {attr}; update {script}"
BASELINE_MALFORMED_ERROR = "{baseline}:{line}: expected `<file> <function> <table> <count>` with a count above zero, got {text!r}"
BASELINE_DUPLICATE_ERROR = "{baseline}:{line}: {file} {function} {table} already has an entry at line {first}"
BASELINE_UNSCANNED_ERROR = "{baseline}:{line}: {file} is not a file this check reads; remove the entry"
BASELINE_STALE_ERROR = "{baseline}:{line}: {file} {function} scans {table} no more; remove the entry"
BASELINE_RISE_ERROR = (
    "{file}: {function} scans {table} {found} time(s), the entry at {baseline}:{line} allows {allowed}; index the new one"
)
BASELINE_DROP_ERROR = (
    "{file}: {function} scans {table} {found} time(s), the entry at {baseline}:{line} allows {allowed}; "
    "lower the entry to {found}"
)
SUMMARY_FAIL = "check_sqlite_query_plans: {findings} finding(s) not pinned and {problems} problem(s)"
SUMMARY_OK = (
    "check_sqlite_query_plans: {statements} statement(s) read across {files} file(s), {planned} planned, "
    "{skipped} skipped on tables the repository does not define, no full scan beyond the {entries} baseline entr(y/ies)"
)
SCRIPT_NAME = "scripts/check_sqlite_query_plans.py"
STATEMENT_PREVIEW_CHARS = 120
# What `--list` prints for a field a verdict has no value for.
LIST_ABSENT = "-"
EXIT_OK = 0
EXIT_FAIL = 1


@dataclass(frozen=True)
class Statement:
    """One execute call the scan saw, with the statement text when it resolved."""

    file: str
    line: int
    function: str
    # The resolved SQL, or None when the argument does not resolve statically.
    sql: str | None

    @property
    def preview(self) -> str:
        if self.sql is None:
            return LIST_ABSENT
        first = next((part.strip() for part in self.sql.splitlines() if part.strip()), "")
        return first[:STATEMENT_PREVIEW_CHARS]


@dataclass(frozen=True)
class Verdict:
    """One classified statement: its kind, and for a scan the table and plan line."""

    statement: Statement
    kind: str
    table: str | None = None
    plan: str | None = None

    @property
    def flagged(self) -> bool:
        return self.kind == KIND_FINDING


@dataclass(frozen=True)
class BaselineEntry:
    """One pinned (file, function, table) count and the baseline line that carries it."""

    file: str
    function: str
    table: str
    count: int
    line: int


class _NullBindings(defaultdict):
    """A mapping that binds NULL to every named parameter."""

    def __missing__(self, key: object) -> None:
        return None


# ---------------------------------------------------------------------------
# Reading the statements


class _Constants:
    """The statically resolvable names of one module, and of the other targets it names."""

    def __init__(self, module: ast.Module, siblings: dict[str, ast.Module]) -> None:
        self.names = _module_constants(module)
        self.siblings = {name: _module_constants(tree) for name, tree in siblings.items()}


def _module_constants(module: ast.Module) -> dict[str, ast.expr]:
    """Module-level names assigned exactly once, with the expression assigned."""
    counts: dict[str, int] = defaultdict(int)
    values: dict[str, ast.expr] = {}
    for node in module.body:
        for name, value in _assigned(node):
            counts[name] += 1
            values[name] = value
    return {name: value for name, value in values.items() if counts[name] == 1}


def _assigned(node: ast.stmt) -> list[tuple[str, ast.expr]]:
    """The (name, expression) pairs a plain assignment binds: `a = x`, `a: T = x`, or `a, b = x, y` element-wise."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        if isinstance(target, ast.Name):
            return [(target.id, node.value)]
        if (
            isinstance(target, (ast.Tuple, ast.List))
            and isinstance(node.value, (ast.Tuple, ast.List))
            and len(target.elts) == len(node.value.elts)
            and all(isinstance(elt, ast.Name) for elt in target.elts)
        ):
            return [(elt.id, value) for elt, value in zip(target.elts, node.value.elts)]  # type: ignore[union-attr]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        return [(node.target.id, node.value)]
    return []


def _local_constants(function: ast.AST) -> dict[str, ast.expr | None]:
    """Every name the function binds: the expression, when bound exactly once by a plain assignment, else None.

    Every binding counts against the name: a store to it (an assignment, an
    unpacking, a loop or `with` target, a walrus, an augmented assignment), a
    parameter, an `except ... as`, an `import ... as`, a `global` or
    `nonlocal`, a `match` capture, or a nested `def` or `class`. A name bound
    any other way, or twice, is in the map with None, so it resolves to
    nothing rather than to the module constant of the same name.
    """
    counts: dict[str, int] = defaultdict(int)
    values: dict[str, ast.expr] = {}
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name, value in _assigned(node):
                values[name] = value
        for name in _bound_names(node):
            counts[name] += 1
    return {name: (values.get(name) if count == 1 else None) for name, count in counts.items()}


def _bound_names(node: ast.AST) -> list[str]:
    """The names one node binds, if any."""
    if isinstance(node, ast.Name):
        return [node.id] if isinstance(node.ctx, ast.Store) else []
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, ast.ExceptHandler):
        return [node.name] if node.name else []
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or alias.name.split(QUALIFIED_SEPARATOR)[0] for alias in node.names]
    if isinstance(node, (ast.Global, ast.Nonlocal)):
        return list(node.names)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return [node.name] if node.name else []
    if isinstance(node, ast.MatchMapping):
        return [node.rest] if node.rest else []
    return []


def _resolve(
    expr: ast.expr, constants: _Constants, local: dict[str, ast.expr | None], depth: int = 0
) -> str | int | float | None:
    """The static value of an expression, or None when it has none."""
    if depth > RESOLVE_DEPTH:
        return None
    if isinstance(expr, ast.Constant) and isinstance(expr.value, (str, int, float)) and not isinstance(expr.value, bool):
        return expr.value
    if isinstance(expr, ast.Name):
        if expr.id in local:
            bound = local[expr.id]
            return None if bound is None else _resolve(bound, constants, local, depth + 1)
        source = constants.names.get(expr.id)
        return None if source is None else _resolve(source, constants, {}, depth + 1)
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        sibling = constants.siblings.get(expr.value.id)
        source = None if sibling is None else sibling.get(expr.attr)
        return None if source is None else _resolve(source, constants, {}, depth + 1)
    if isinstance(expr, ast.JoinedStr):
        parts: list[str] = []
        for value in expr.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue) and value.format_spec is None and value.conversion == -1:
                inner = _resolve(value.value, constants, local, depth + 1)
                if inner is None:
                    return None
                parts.append(str(inner))
            else:
                return None
        return "".join(parts)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == STR_CALL:
        if len(expr.args) != 1 or expr.keywords:
            return None
        inner = _resolve(expr.args[0], constants, local, depth + 1)
        return None if inner is None else str(inner)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == JOIN_METHOD:
        if len(expr.args) != 1 or expr.keywords:
            return None
        separator = _resolve(expr.func.value, constants, local, depth + 1)
        items = _sequence_items(expr.args[0], constants, local, depth + 1)
        if not isinstance(separator, str) or items is None:
            return None
        parts = [_resolve(item, constants, local, depth + 1) for item in items]
        if not all(isinstance(part, str) for part in parts):
            return None
        return separator.join(parts)  # type: ignore[arg-type]
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _resolve(expr.left, constants, local, depth + 1)
        right = _resolve(expr.right, constants, local, depth + 1)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        return None
    return None


def _sequence_items(
    expr: ast.expr, constants: _Constants, local: dict[str, ast.expr | None], depth: int
) -> list[ast.expr] | None:
    """The elements of a tuple literal, named directly or through a constant, or None.

    A tuple only: a list literal bound once can still be appended to before
    the join (`assignments = []` ... `assignments.append(...)`), so its
    literal is not its value.
    """
    if depth > RESOLVE_DEPTH:
        return None
    if isinstance(expr, ast.Tuple):
        return list(expr.elts)
    if isinstance(expr, ast.Name):
        if expr.id in local:
            bound = local[expr.id]
            return None if bound is None else _sequence_items(bound, constants, local, depth + 1)
        source = constants.names.get(expr.id)
        return None if source is None else _sequence_items(source, constants, {}, depth + 1)
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        sibling = constants.siblings.get(expr.value.id)
        source = None if sibling is None else sibling.get(expr.attr)
        return None if source is None else _sequence_items(source, constants, {}, depth + 1)
    return None


class _Collector(ast.NodeVisitor):
    """Every execute call in a module, named by the function it sits in."""

    def __init__(self, shown: str, constants: _Constants) -> None:
        self.shown = shown
        self.constants = constants
        self.stack: list[str] = []
        self.locals: list[dict[str, ast.expr | None]] = []
        self.statements: list[Statement] = []

    def _scoped(self, node: ast.AST, name: str, local: dict[str, ast.expr | None] | None) -> None:
        self.stack.append(name)
        if local is not None:
            self.locals.append(local)
        self.generic_visit(node)
        if local is not None:
            self.locals.pop()
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name, None)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name, _local_constants(node))

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in EXECUTE_METHODS and node.args:
            local: dict[str, ast.expr | None] = self.locals[-1] if self.locals else {}
            value = _resolve(node.args[0], self.constants, local)
            sql = value if isinstance(value, str) else None
            function = QUALIFIED_SEPARATOR.join(self.stack) or MODULE_LEVEL
            self.statements.append(Statement(self.shown, node.lineno, function, sql))
        self.generic_visit(node)


def scan_statements(path: Path, root: Path, siblings: dict[str, ast.Module]) -> list[Statement]:
    """Every execute call in one module, with its statement where it resolves."""
    shown = path.relative_to(root).as_posix()
    module = ast.parse(path.read_text(encoding="utf-8"), filename=shown)
    others = {name: tree for name, tree in siblings.items() if name != path.stem}
    collector = _Collector(shown, _Constants(module, others))
    collector.visit(module)
    return collector.statements


def scan(root: Path, targets: Iterable[str]) -> tuple[list[Statement], list[str], list[str]]:
    """Every statement across the targets, the files read (repository-relative), and the targets that were not there."""
    paths: list[Path] = []
    problems: list[str] = []
    for rel in targets:
        path = root / rel
        if not path.is_file():
            problems.append(TARGET_MISSING_ERROR.format(target=rel, script=SCRIPT_NAME))
            continue
        paths.append(path)
    trees = {path.stem: ast.parse(path.read_text(encoding="utf-8"), filename=str(path)) for path in paths}
    statements: list[Statement] = []
    files: list[str] = []
    for path in paths:
        files.append(path.relative_to(root).as_posix())
        statements.extend(scan_statements(path, root, trees))
    return statements, files, problems


# ---------------------------------------------------------------------------
# Building the schema and planning


def build_schema(root: Path, source: SchemaSource, directory: Path) -> tuple[sqlite3.Connection | None, list[str]]:
    """A connection to a fresh database holding the module's schema, or the problems that stopped it."""
    module_path = root / source.module
    shown = source.module
    if not module_path.is_file():
        return None, [TARGET_MISSING_ERROR.format(target=shown, script=SCRIPT_NAME)]
    db_path = directory / SCHEMA_DB_FILENAME
    previous_env = os.environ.get(source.db_path_attr)
    os.environ[source.db_path_attr] = str(db_path)
    module_dir = str(module_path.parent)
    inserted = module_dir not in sys.path
    if inserted:
        sys.path.insert(0, module_dir)
    try:
        spec = importlib.util.spec_from_file_location(module_path.stem, module_path)
        if spec is None or spec.loader is None:
            return None, [SCHEMA_IMPORT_ERROR.format(module=shown, error="no loader")]
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_path.stem] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # any import failure is the finding
            return None, [SCHEMA_IMPORT_ERROR.format(module=shown, error=f"{type(exc).__name__}: {exc}")]
        for attr in (source.db_path_attr, source.init_function, *source.extra_ddl_attrs):
            if not hasattr(module, attr):
                return None, [SCHEMA_ATTR_ERROR.format(module=shown, attr=attr, script=SCRIPT_NAME)]
        setattr(module, source.db_path_attr, str(db_path))
        getattr(module, source.init_function)()
        conn = sqlite3.connect(str(db_path))
        for attr in source.extra_ddl_attrs:
            conn.execute(getattr(module, attr))
        conn.commit()
        return conn, []
    finally:
        if inserted:
            sys.path.remove(module_dir)
        if previous_env is None:
            os.environ.pop(source.db_path_attr, None)
        else:
            os.environ[source.db_path_attr] = previous_env


def schema_tables(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return frozenset(row[0] for row in rows)


def leading_keyword(sql: str) -> str:
    """The first SQL word, with leading `--` comments skipped, upper-cased."""
    for raw in sql.splitlines():
        line = raw.strip()
        if not line or line.startswith(SQL_LINE_COMMENT):
            continue
        return re.split(r"[\s(;]", line, maxsplit=1)[0].upper()
    return ""


def explain(conn: sqlite3.Connection, sql: str) -> list[str]:
    """The plan lines of one statement, prepared with NULL bound to every parameter.

    Named parameters take a mapping that answers NULL to every name; sqlite3
    refuses a mapping for `?` parameters, so those are bound by position, with
    the count read from the error a bare prepare raises. The mapping goes
    first because a sequence bound to named parameters is a deprecation on
    Python 3.12 and an error on 3.14.
    """
    text = EXPLAIN_PREFIX + sql
    try:
        rows = conn.execute(text, _NullBindings()).fetchall()
    except sqlite3.ProgrammingError:
        try:
            rows = conn.execute(text, ()).fetchall()
        except sqlite3.ProgrammingError as exc:
            match = BINDINGS_RE.search(str(exc))
            if match is None:
                raise
            rows = conn.execute(text, (None,) * int(match.group(1))).fetchall()
    return [row[PLAN_DETAIL_COLUMN] for row in rows]


def scanned_table(plan: str) -> str | None:
    """The table a plan line reads in full with no index, or None."""
    match = SCAN_RE.match(plan)
    if match is None:
        return None
    rest = match.group("rest") + " "
    if any(marker in rest for marker in INDEXED_SCAN_MARKERS):
        return None
    return match.group("name")


def table_aliases(sql: str, tables: frozenset[str]) -> dict[str, str]:
    """alias -> table for every aliased reference to a schema table in the statement."""
    found: dict[str, str] = {}
    for match in ALIAS_RE.finditer(sql):
        table, alias = match.group("table"), match.group("alias")
        if alias is not None and table in tables:
            found[alias] = table
    return found


def cte_names(sql: str) -> frozenset[str]:
    """The common table expressions the statement declares."""
    return frozenset(match.group("name") for match in CTE_RE.finditer(sql))


def _non_table_scan(name: str, ctes: frozenset[str]) -> bool:
    """Whether a scan name is a constant row, an unflattened subquery or a CTE rather than a table."""
    return name in NON_TABLE_SCAN_NAMES or name.isdecimal() or name.startswith(SUBQUERY_SCAN_PREFIXES) or name in ctes


def judge(statements: Iterable[Statement], conn: sqlite3.Connection) -> tuple[list[Verdict], list[str]]:
    """Every statement classified, and the statements that do not prepare for a reason other than a foreign table."""
    tables = schema_tables(conn)
    verdicts: list[Verdict] = []
    problems: list[str] = []
    for statement in statements:
        if statement.sql is None:
            verdicts.append(Verdict(statement, KIND_DYNAMIC))
            continue
        if leading_keyword(statement.sql) in SCHEMA_KEYWORDS:
            verdicts.append(Verdict(statement, KIND_SCHEMA))
            continue
        try:
            plans = explain(conn, statement.sql)
        except sqlite3.Error as exc:
            message = str(exc)
            if message.startswith(NO_SUCH_TABLE_PREFIX):
                verdicts.append(Verdict(statement, KIND_SKIPPED, table=message[len(NO_SUCH_TABLE_PREFIX) :]))
            else:
                verdicts.append(Verdict(statement, KIND_ERROR, plan=message))
                problems.append(
                    PREPARE_ERROR.format(
                        file=statement.file,
                        line=statement.line,
                        function=statement.function,
                        statement=statement.preview,
                        error=message,
                    )
                )
            continue
        judged = 0
        aliases = table_aliases(statement.sql, tables)
        ctes = cte_names(statement.sql)
        for plan in plans:
            name = scanned_table(plan)
            if name is None or name in SCHEMA_TABLES:
                continue
            table = name if name in tables else aliases.get(name)
            if table is None:
                if _non_table_scan(name, ctes):
                    continue
                # A name the statement did not declare is a scan the check
                # cannot place: loud, so an alias form the regex does not
                # read cannot pass as clean.
                judged += 1
                verdicts.append(Verdict(statement, KIND_ERROR, plan=plan))
                problems.append(
                    PLAN_NAME_ERROR.format(
                        file=statement.file,
                        line=statement.line,
                        function=statement.function,
                        plan=plan,
                        name=name,
                        script=SCRIPT_NAME,
                    )
                )
                continue
            judged += 1
            kind = KIND_BOUNDED if table in BOUNDED_TABLES else KIND_FINDING
            verdicts.append(Verdict(statement, kind, table=table, plan=plan))
        if judged == 0:
            verdicts.append(Verdict(statement, KIND_OK))
    return verdicts, problems


# ---------------------------------------------------------------------------
# The baseline


def display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def load_baseline(path: Path, shown: str) -> tuple[list[BaselineEntry], list[str]]:
    """Every entry of the baseline, and the lines that are not one."""
    entries: list[BaselineEntry] = []
    errors: list[str] = []
    first_seen: dict[tuple[str, str, str], int] = {}
    if not path.is_file():
        return entries, errors
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(COMMENT_PREFIX):
            continue
        fields = line.split()
        count = int(fields[3]) if len(fields) == BASELINE_FIELDS and fields[3].isdecimal() else 0
        if count <= 0:
            errors.append(BASELINE_MALFORMED_ERROR.format(baseline=shown, line=number, text=line))
            continue
        key = (fields[0], fields[1], fields[2])
        if key in first_seen:
            errors.append(
                BASELINE_DUPLICATE_ERROR.format(
                    baseline=shown, line=number, file=key[0], function=key[1], table=key[2], first=first_seen[key]
                )
            )
            continue
        first_seen[key] = number
        entries.append(BaselineEntry(key[0], key[1], key[2], count, number))
    return entries, errors


def reconcile(
    verdicts: list[Verdict], files: list[str], entries: list[BaselineEntry], shown: str
) -> tuple[list[Verdict], list[str]]:
    """Flagged scans no entry covers, and the entries the source no longer matches exactly."""
    flagged: dict[tuple[str, str, str], list[Verdict]] = {}
    for verdict in verdicts:
        if verdict.flagged and verdict.table is not None:
            key = (verdict.statement.file, verdict.statement.function, verdict.table)
            flagged.setdefault(key, []).append(verdict)
    problems: list[str] = []
    covered: set[tuple[str, str, str]] = set()
    scanned = set(files)
    for entry in entries:
        key = (entry.file, entry.function, entry.table)
        fields = dict(
            baseline=shown, line=entry.line, file=entry.file, function=entry.function, table=entry.table, allowed=entry.count
        )
        if entry.file not in scanned:
            problems.append(BASELINE_UNSCANNED_ERROR.format(**fields))
            continue
        found = len(flagged.get(key, ()))
        covered.add(key)
        if found == 0:
            problems.append(BASELINE_STALE_ERROR.format(**fields))
        elif found > entry.count:
            problems.append(BASELINE_RISE_ERROR.format(found=found, **fields))
        elif found < entry.count:
            problems.append(BASELINE_DROP_ERROR.format(found=found, **fields))
    findings = [v for key, group in sorted(flagged.items()) if key not in covered for v in group]
    return findings, problems


# ---------------------------------------------------------------------------
# The check


def check(
    root: Path = REPO_ROOT,
    targets: Iterable[str] = TARGETS,
    schema: SchemaSource = SCHEMA_SOURCE,
    baseline: Path = BASELINE,
    list_statements: bool = False,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    shown = display_path(baseline, root)
    statements, files, problems = scan(root, targets)
    entries, malformed = load_baseline(baseline, shown)
    problems.extend(malformed)
    verdicts: list[Verdict] = []
    findings: list[Verdict] = []
    with tempfile.TemporaryDirectory() as directory:
        conn, schema_problems = build_schema(root, schema, Path(directory))
        problems.extend(schema_problems)
        if conn is not None:
            try:
                verdicts, prepare_problems = judge(statements, conn)
            finally:
                conn.close()
            problems.extend(prepare_problems)
            findings, baseline_problems = reconcile(verdicts, files, entries, shown)
            problems.extend(baseline_problems)
    if list_statements:
        for verdict in verdicts:
            print(
                LIST_LINE.format(
                    file=verdict.statement.file,
                    line=verdict.statement.line,
                    function=verdict.statement.function,
                    kind=verdict.kind,
                    table=verdict.table or LIST_ABSENT,
                    plan=verdict.plan or LIST_ABSENT,
                    statement=verdict.statement.preview,
                ),
                file=out,
            )
    for verdict in findings:
        print(
            FINDING_LINE.format(
                file=verdict.statement.file,
                line=verdict.statement.line,
                function=verdict.statement.function,
                table=verdict.table,
                plan=verdict.plan,
                statement=verdict.statement.preview,
                baseline=shown,
            ),
            file=err,
        )
    for problem in problems:
        print(problem, file=err)
    if findings or problems:
        print(SUMMARY_FAIL.format(findings=len(findings), problems=len(problems)), file=err)
        return EXIT_FAIL
    judged = {id(v.statement) for v in verdicts if v.kind in (KIND_OK, KIND_FINDING, KIND_BOUNDED)}
    skipped = sum(1 for v in verdicts if v.kind == KIND_SKIPPED)
    print(
        SUMMARY_OK.format(
            statements=len(statements), files=len(files), planned=len(judged), skipped=skipped, entries=len(entries)
        ),
        file=out,
    )
    return EXIT_OK


def main(argv: list[str] | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root the targets are relative to")
    parser.add_argument("--target", action="append", default=None, help="a Python module to read (repeatable; default: the targets)")
    parser.add_argument("--baseline", type=Path, default=None, help="the baseline file (default: scripts/sqlite_query_plan_baseline.txt)")
    parser.add_argument("--list", action="store_true", help="print every statement with its classification")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    targets = tuple(args.target) if args.target is not None else TARGETS
    baseline = args.baseline if args.baseline is not None else (BASELINE if root == REPO_ROOT else root / BASELINE.relative_to(REPO_ROOT))
    return check(root, targets, SCHEMA_SOURCE, baseline, list_statements=args.list, out=out, err=err)


if __name__ == "__main__":
    sys.exit(main())
