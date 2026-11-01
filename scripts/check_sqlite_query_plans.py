#!/usr/bin/env python3
"""Fail when a SQL statement the session server issues would scan a whole table.

Two full scans shipped unnoticed: the ``intercepted_events`` fold lookup and
the ``session_metadata`` retention delete. Nothing read their query plans, so
this does. It collects every SQL literal in a fixed roster of source files,
builds the schema the way the server does at startup (``init_db``, migrations
and partial indexes included, rather than a second copy of the DDL that would
drift), runs each statement under ``EXPLAIN QUERY PLAN`` with null bindings,
and fails on any ``SCAN <table>`` line on one of the server's own unbounded
tables that is not ``USING INDEX`` or ``USING COVERING INDEX``.

What it reads
-------------
A string expression whose first word is an uppercase SQL keyword: a plain
literal, an implicitly or ``+``-concatenated one, or an f-string. An f-string
placeholder that names a module-level constant is substituted with the
constant's value, which is what makes a partial index's ``WHERE reason = '…'``
match the query text the way it does at runtime; any other placeholder becomes
``?``. A statement that still does not parse is listed as dynamic, not planned.
Docstrings are skipped.

What it decides
---------------
- A scan on a table the schema defines, not on the bounded-table allow-list and
  not in the baseline, fails the check.
- The baseline (``scripts/sqlite_query_plan_baseline.txt``) holds today's known
  scans, one normalised statement per entry under ``#`` lines naming the issue
  that owns the fix. It only shrinks: an entry whose statement no longer scans
  fails until it is removed, so the file cannot outlive the gap it records.
- A statement on a table the schema does not define (Hermes owns ``sessions``,
  ``messages``, ``tasks``, ``task_events`` and ``executions``, and its DDL lives
  in the image) is listed as skipped, naming the table.
- A statement on an owned table that fails to plan for any other reason (a
  renamed column, say) is an error, so a broken query cannot pass by silence.
- DDL, ``PRAGMA`` and transaction control are listed as schema statements and
  not planned; the planner has nothing to say about them.

Needs the platform scripts' imports (``make test-python-deps``), because the
schema comes from importing the server module. No network; well under a second
of planning.

Usage::

    python3 scripts/check_sqlite_query_plans.py [--baseline PATH]
"""

from __future__ import annotations

import argparse
import ast
import importlib
import os
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The files whose SQL is collected. The server and its findings queue own the
# schema; the two state.db helpers issue read-only statements against Hermes's
# tables, which are listed as skipped so a reader sees they were seen.
SQL_SOURCES = (
    "agents/platform/scripts/session_kv_server.py",
    "agents/platform/scripts/findings_queue.py",
    "agents/platform/scripts/session_retention.py",
    "agents/platform/scripts/credential_proxy.py",
)

# Where the schema comes from: importing this module and calling its init_db()
# on a temporary database, as its own tests do, so the migrations and the
# f-string partial indexes are the ones that run rather than a transcription.
SCRIPTS_DIR = REPO_ROOT / "agents" / "platform" / "scripts"
SCHEMA_MODULE = "session_kv_server"
SCHEMA_INIT_FUNCTION = "init_db"
SCHEMA_DB_PATH_ATTR = "SESSION_KV_DB_PATH"
SCHEMA_DB_FILENAME = "session_kv.db"

DEFAULT_BASELINE = REPO_ROOT / "scripts" / "sqlite_query_plan_baseline.txt"
BASELINE_COMMENT_PREFIX = "#"

# Tables bounded by design, so a scan over them reads a handful of rows. Each
# reason is the shape that bounds the table; a table that grows with traffic
# does not belong here.
BOUNDED_TABLES = {
    "drift_backlog_lease": "one row per lease name, and there is one lease",
    "data_volume_warning": "one row, enforced by CHECK (id = 1)",
    "alert_quota": "one row per (day, severity), deleted after the retention window",
    "audit_report_runs": "one row per (profile, job_id): the scheduled audit jobs",
    "scheduler_failure_posts": "one row per (profile, job_id): the scheduled jobs",
    "queue_publications": "one row per publisher",
}

# Statements the planner has an opinion about.
PLANNED_KEYWORDS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "WITH"})
# Statements that are collected and listed, never planned.
SCHEMA_KEYWORDS = frozenset(
    {"CREATE", "DROP", "ALTER", "PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "VACUUM", "ANALYZE", "REINDEX", "SAVEPOINT", "RELEASE"}
)
SQL_KEYWORDS = PLANNED_KEYWORDS | SCHEMA_KEYWORDS

# What an unresolvable f-string placeholder becomes. A bound parameter, so the
# statement still parses wherever a value may stand; where a table or column
# name stood, the statement stops parsing and is listed as dynamic.
PLACEHOLDER = "?"

EXPLAIN_PREFIX = "EXPLAIN QUERY PLAN "
# `SCAN <name>` optionally followed by how: `USING INDEX …`, `USING COVERING
# INDEX …`, or nothing, which is the full scan this check exists to catch.
SCAN_RE = re.compile(r"^SCAN (\S+)(?: (.*))?$")
INDEXED_MARKERS = ("USING INDEX", "USING COVERING INDEX")
NO_SUCH_TABLE_RE = re.compile(r"no such table: (\S+)")
BINDINGS_RE = re.compile(r"statement uses (\d+)")
# sqlite's own messages for a statement that is not SQL as written.
SYNTAX_ERROR_MARKERS = ("syntax error", "incomplete input", "unrecognized token")
SQLITE_INTERNAL_TABLE_PREFIX = "sqlite_"
OWNED_TABLES_SQL = "SELECT name FROM sqlite_master WHERE type = 'table'"
# Words that follow a table name without being its alias.
ALIAS_STOPWORDS = frozenset(
    {
        "WHERE", "SET", "ON", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "NATURAL", "ORDER", "GROUP",
        "LIMIT", "USING", "AND", "OR", "VALUES", "SELECT", "FROM", "DEFAULT", "ADD", "RENAME", "DROP", "HAVING",
        "UNION", "EXCEPT", "INTERSECT", "WINDOW", "RETURNING", "NOT", "INDEXED", "AS", "IS", "IN", "WHEN",
    }
)
ALIAS_RE_TEMPLATE = r"\b{table}\s+(?:AS\s+)?([A-Za-z_]\w*)"
# ast.NodeVisitor's method naming, repeated because _SqlCollector overrides visit().
VISITOR_METHOD_PREFIX = "visit_"

WHITESPACE_RE = re.compile(r"\s+")
# How much of a statement a listing line shows.
LISTING_WIDTH = 110
ELLIPSIS = "..."
SCAN_SEPARATOR = "; "

# Result kinds.
KIND_CLEAN = "clean"
KIND_SCAN = "scan"
KIND_SKIPPED = "skipped"
KIND_DYNAMIC = "dynamic"
KIND_SCHEMA = "schema"
KIND_ERROR = "error"


@dataclass(frozen=True)
class Statement:
    path: str
    line: int
    sql: str

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line}"

    @property
    def normalized(self) -> str:
        return normalize(self.sql)


@dataclass(frozen=True)
class Scan:
    table: str
    detail: str


@dataclass
class PlanResult:
    statement: Statement
    kind: str
    scans: tuple[Scan, ...] = ()
    message: str = ""


@dataclass
class Report:
    """Every result, and the scans sorted three ways.

    `new_scans` and `baselined_scans` hold one entry per statement with the
    scans on unbounded tables it produced (a subquery can add a second), since
    the baseline keys on the statement; `allowed_scans` holds one per scan.
    """

    results: list[PlanResult] = field(default_factory=list)
    new_scans: list[tuple[PlanResult, tuple[Scan, ...]]] = field(default_factory=list)
    baselined_scans: list[tuple[PlanResult, tuple[Scan, ...]]] = field(default_factory=list)
    allowed_scans: list[tuple[PlanResult, Scan]] = field(default_factory=list)
    stale_baseline: list[str] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[PlanResult]:
        return [r for r in self.results if r.kind == kind]

    @property
    def ok(self) -> bool:
        return not self.new_scans and not self.stale_baseline and not self.of_kind(KIND_ERROR)


def normalize(sql: str) -> str:
    """One line, single spaces: the form the baseline keys on."""
    return WHITESPACE_RE.sub(" ", sql).strip()


def leading_keyword(sql: str) -> str:
    parts = sql.split(None, 1)
    return parts[0] if parts else ""


def looks_like_sql(text: str) -> bool:
    """An uppercase SQL keyword first, and something after it."""
    parts = text.split(None, 1)
    return len(parts) == 2 and parts[0] in SQL_KEYWORDS


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def _constant_text(node: ast.AST, constants: dict[str, str]) -> str:
    """What a placeholder or an operand renders to inside a statement."""
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    return PLACEHOLDER


def render_string(node: ast.AST, constants: dict[str, str]) -> str | None:
    """The text of a string expression, or None when the node is not one.

    Covers a literal, an f-string (module constants substituted, everything else
    ``?``), and ``+`` concatenation where at least one side is a string.
    Adjacent literals arrive from the parser already joined.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue):
                parts.append(_constant_text(value.value, constants))
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = render_string(node.left, constants)
        right = render_string(node.right, constants)
        if left is None and right is None:
            return None
        if left is None:
            left = _constant_text(node.left, constants)
        if right is None:
            right = _constant_text(node.right, constants)
        return left + right
    return None


def module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level names bound to a literal, by their rendered text.

    Strings and numbers count; an f-string constant is rendered with the
    constants bound before it, so ``_SELECT`` built from a column tuple becomes
    ``SELECT ? FROM findings`` and the statements that extend it still parse.
    """
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            name, value = node.target.id, node.value
        else:
            continue
        text = render_string(value, constants)
        if text is None and isinstance(value, ast.Constant) and isinstance(value.value, (int, float)):
            text = str(value.value)
        if text is not None:
            constants[name] = text
    return constants


class _SqlCollector(ast.NodeVisitor):
    def __init__(self, path: str, constants: dict[str, str]) -> None:
        self.path = path
        self.constants = constants
        self.found: list[Statement] = []

    def visit(self, node: ast.AST) -> None:
        text = render_string(node, self.constants)
        if text is not None:
            if looks_like_sql(text):
                self.found.append(Statement(self.path, getattr(node, "lineno", 0), text))
            return
        # NodeVisitor.visit's dispatch, repeated here because this override
        # replaces it: visit_Expr below is where docstrings are dropped.
        getattr(self, VISITOR_METHOD_PREFIX + node.__class__.__name__, self.generic_visit)(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        # A bare string statement is a docstring (or a no-op), not SQL.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)


def collect_statements(path: str, source: str) -> list[Statement]:
    tree = ast.parse(source, filename=path)
    collector = _SqlCollector(path, module_constants(tree))
    collector.visit(tree)
    return collector.found


def collect_sources(root: Path = REPO_ROOT, sources: tuple[str, ...] = SQL_SOURCES) -> list[Statement]:
    statements: list[Statement] = []
    for relative in sources:
        file = root / relative
        if not file.is_file():
            raise FileNotFoundError(f"{relative} is on the roster and missing")
        statements.extend(collect_statements(relative, file.read_text(encoding="utf-8")))
    return statements


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


def build_schema(work_dir: Path) -> sqlite3.Connection:
    """The server's own schema, built by its init_db() on a database under work_dir."""
    db_path = work_dir / SCHEMA_DB_FILENAME
    # Before the import: the module reads the variable at import time. After it:
    # a module already imported in this process keeps its earlier value.
    os.environ[SCHEMA_DB_PATH_ATTR] = str(db_path)
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    module = importlib.import_module(SCHEMA_MODULE)
    setattr(module, SCHEMA_DB_PATH_ATTR, str(db_path))
    getattr(module, SCHEMA_INIT_FUNCTION)()
    return sqlite3.connect(db_path)


def owned_tables(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute(OWNED_TABLES_SQL).fetchall()
    return frozenset(name for (name,) in rows if not name.startswith(SQLITE_INTERNAL_TABLE_PREFIX))


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def _explain(conn: sqlite3.Connection, sql: str) -> list[tuple]:
    """EXPLAIN QUERY PLAN rows, with as many null bindings as the statement wants."""
    query = EXPLAIN_PREFIX + sql
    try:
        return conn.execute(query).fetchall()
    except sqlite3.ProgrammingError as exc:
        match = BINDINGS_RE.search(str(exc))
        if match is None:
            raise
        return conn.execute(query, (None,) * int(match.group(1))).fetchall()


def table_aliases(sql: str, owned: frozenset[str]) -> dict[str, str]:
    """Alias -> owned table, because the planner names a table by its alias."""
    aliases: dict[str, str] = {}
    for table in owned:
        for match in re.finditer(ALIAS_RE_TEMPLATE.format(table=re.escape(table)), sql, re.IGNORECASE):
            alias = match.group(1)
            if alias.upper() not in ALIAS_STOPWORDS and alias not in owned:
                aliases[alias] = table
    return aliases


def full_scans(sql: str, rows: list[tuple], owned: frozenset[str]) -> tuple[Scan, ...]:
    aliases = table_aliases(sql, owned)
    scans = []
    for row in rows:
        detail = row[-1]
        match = SCAN_RE.match(detail)
        if match is None:
            continue
        name, how = match.group(1), match.group(2) or ""
        if any(marker in how for marker in INDEXED_MARKERS):
            continue
        table = name if name in owned else aliases.get(name)
        if table is not None:
            scans.append(Scan(table, detail))
    return tuple(scans)


def plan_statement(conn: sqlite3.Connection, statement: Statement, owned: frozenset[str]) -> PlanResult:
    keyword = leading_keyword(statement.sql).upper()
    if keyword not in PLANNED_KEYWORDS:
        return PlanResult(statement, KIND_SCHEMA)
    try:
        rows = _explain(conn, statement.sql)
    except sqlite3.Error as exc:
        message = str(exc)
        match = NO_SUCH_TABLE_RE.search(message)
        if match is not None and match.group(1) not in owned:
            return PlanResult(statement, KIND_SKIPPED, message=match.group(1))
        if any(marker in message for marker in SYNTAX_ERROR_MARKERS):
            return PlanResult(statement, KIND_DYNAMIC, message=message)
        return PlanResult(statement, KIND_ERROR, message=message)
    scans = full_scans(statement.sql, rows, owned)
    return PlanResult(statement, KIND_SCAN if scans else KIND_CLEAN, scans=scans)


def plan_statements(
    conn: sqlite3.Connection,
    statements: list[Statement],
    baseline: dict[str, str],
    bounded: dict[str, str] = BOUNDED_TABLES,
) -> Report:
    owned = owned_tables(conn)
    report = Report()
    matched: set[str] = set()
    for statement in statements:
        result = plan_statement(conn, statement, owned)
        report.results.append(result)
        report.allowed_scans.extend((result, scan) for scan in result.scans if scan.table in bounded)
        unbounded = tuple(scan for scan in result.scans if scan.table not in bounded)
        if not unbounded:
            continue
        if statement.normalized in baseline:
            matched.add(statement.normalized)
            report.baselined_scans.append((result, unbounded))
        else:
            report.new_scans.append((result, unbounded))
    report.stale_baseline = [entry for entry in baseline if entry not in matched]
    return report


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------


def parse_baseline(text: str) -> dict[str, str]:
    """Statement -> reason. Comment lines above an entry are its reason."""
    baseline: dict[str, str] = {}
    reason: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            reason = []
            continue
        if line.startswith(BASELINE_COMMENT_PREFIX):
            reason.append(line.lstrip(BASELINE_COMMENT_PREFIX).strip())
            continue
        if line in baseline:
            raise ValueError(f"baseline lists a statement twice: {line}")
        if not reason:
            raise ValueError(f"baseline entry has no reason above it: {line}")
        baseline[line] = " ".join(reason)
    return baseline


def load_baseline(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse_baseline(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def _clip(text: str) -> str:
    text = normalize(text)
    if len(text) <= LISTING_WIDTH:
        return text
    return text[: LISTING_WIDTH - len(ELLIPSIS)] + ELLIPSIS


def _details(scans: tuple[Scan, ...]) -> str:
    return SCAN_SEPARATOR.join(scan.detail for scan in scans)


def render_report(report: Report, baseline: dict[str, str]) -> str:
    lines: list[str] = []
    planned = [r for r in report.results if r.kind in (KIND_CLEAN, KIND_SCAN)]
    lines.append(
        f"sqlite query plans: {len(report.results)} statements; {len(planned)} planned, "
        f"{len(report.of_kind(KIND_SCHEMA))} schema/transaction, {len(report.of_kind(KIND_DYNAMIC))} dynamic, "
        f"{len(report.of_kind(KIND_SKIPPED))} skipped, {len(report.of_kind(KIND_ERROR))} errors"
    )
    skipped = report.of_kind(KIND_SKIPPED)
    if skipped:
        lines.append("")
        lines.append("skipped: tables this repository defines no DDL for")
        for result in skipped:
            lines.append(f"  {result.statement.where}  table {result.message}  {_clip(result.statement.sql)}")
    dynamic = report.of_kind(KIND_DYNAMIC)
    if dynamic:
        lines.append("")
        lines.append("dynamic: not SQL as written, not planned")
        for result in dynamic:
            lines.append(f"  {result.statement.where}  {_clip(result.statement.sql)}")
    if report.allowed_scans:
        lines.append("")
        lines.append("scans on bounded tables (allowed)")
        for result, scan in report.allowed_scans:
            lines.append(f"  {result.statement.where}  {scan.detail}  {_clip(result.statement.sql)}")
    if report.baselined_scans:
        lines.append("")
        lines.append("scans in the baseline (known)")
        for result, scans in report.baselined_scans:
            lines.append(f"  {result.statement.where}  {_details(scans)}  {_clip(result.statement.sql)}")
            lines.append(f"      {baseline[result.statement.normalized]}")
    errors = report.of_kind(KIND_ERROR)
    if errors:
        lines.append("")
        lines.append("ERROR: statements on owned tables that failed to plan")
        for result in errors:
            lines.append(f"  {result.statement.where}  {result.message}")
            lines.append(f"      {_clip(result.statement.sql)}")
    if report.new_scans:
        lines.append("")
        lines.append("FAIL: full scans not in the baseline")
        for result, scans in report.new_scans:
            lines.append(f"  {result.statement.where}  {_details(scans)}")
            lines.append(f"      {result.statement.normalized}")
        lines.append("  Add an index, or add the statement to the baseline under a reason naming the issue that owns the fix.")
    if report.stale_baseline:
        lines.append("")
        lines.append("FAIL: baseline entries that no longer scan (remove them)")
        for entry in report.stale_baseline:
            lines.append(f"  {entry}")
    lines.append("")
    lines.append("sqlite query plans: OK" if report.ok else "sqlite query plans: FAIL")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def run(baseline_path: Path, out=sys.stdout) -> int:
    started = time.monotonic()
    baseline = load_baseline(baseline_path)
    statements = collect_sources()
    if not statements:
        print("sqlite query plans: no SQL statement found in the roster; the extractor is broken", file=out)
        return 1
    with tempfile.TemporaryDirectory() as work:
        conn = build_schema(Path(work))
        try:
            report = plan_statements(conn, statements, baseline)
        finally:
            conn.close()
    print(render_report(report, baseline), file=out)
    print(f"({time.monotonic() - started:.1f}s)", file=out)
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE, help="known full scans, one per entry")
    args = parser.parse_args(argv)
    return run(args.baseline)


if __name__ == "__main__":
    sys.exit(main())
