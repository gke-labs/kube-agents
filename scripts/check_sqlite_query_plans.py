#!/usr/bin/env python3
"""Fail when a SQL statement against the session server's tables would scan a whole table.

Two full scans shipped unnoticed: the ``intercepted_events`` fold lookup and
the ``session_metadata`` retention delete. Nothing read their query plans, so
this does. It collects every SQL literal in the files that issue them, builds
the schema the way the server does at startup (``init_db``, migrations and
partial indexes included, rather than a second copy of the DDL that would
drift), runs each statement under ``EXPLAIN QUERY PLAN`` with null bindings,
and fails on any ``SCAN <table>`` line on one of the server's own unbounded
tables that is not ``USING INDEX`` or ``USING COVERING INDEX``.

Which files
-----------
A fixed roster (the server, its findings queue, and the two state.db helpers)
is read whole, so its statements on Hermes-owned tables are listed as skipped.
Beyond the roster, every tracked Python file outside tests, verifiers, the
bench harness and third-party trees is searched, and any statement in it that
names one of the owned tables is planned too: the chat agent's session-store
plugin and the Slack incident patch write and read the same database, and a
new consumer joins the gate the moment it names a table. A Python program held
in a string (the admin console runs such programs in the pod) is parsed for
its SQL as well.

What it reads
-------------
A string expression whose first word is an uppercase SQL keyword: a plain
literal, an implicitly or ``+``-concatenated one, or an f-string. An f-string
placeholder that names a module-level constant is substituted with the
constant's value, which is what makes a partial index's ``WHERE reason = '…'``
match the query text the way it does at runtime; any other placeholder becomes
``?``. Docstrings are skipped.

What it decides
---------------
- A scan on an owned table that is not on the bounded-table allow-list and
  not in the baseline fails the check.
- A statement on an owned table that does not parse as written (a SET list or
  an IN list built at runtime) is dynamic: the planner cannot see it, so it
  too must be in the baseline, under a reason saying why its shape is safe.
- The baseline (``scripts/sqlite_query_plan_baseline.txt``) keys each entry on
  the file and the normalised statement, one line per site, under ``#`` lines
  naming the issue that owns the fix. It only shrinks: an entry whose statement
  no longer scans, or covers more sites than exist, fails until it is edited,
  and a second site of a known statement is a new finding.
- A statement on one of Hermes's tables (``sessions``, ``messages``, ``tasks``
  and the rest of ``KNOWN_FOREIGN_TABLES``, whose DDL lives in the image) is
  listed as skipped, naming the table. Any other unknown table is an error, so
  a renamed or misspelt owned table cannot pass as foreign.
- A statement on an owned table that fails to plan for any other reason (a
  renamed column, say) is an error, so a broken query cannot pass by silence.
- DDL, ``PRAGMA`` and transaction control are listed as schema statements and
  not planned; the planner has nothing to say about them.

Needs the platform scripts' imports (``make test-python-deps``), because the
schema comes from importing the server module. No network; a few seconds.

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
SELF_PATH = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

# The files read whole. The server and its findings queue own the schema; the
# two state.db helpers issue read-only statements against Hermes's tables,
# which are listed as skipped so a reader sees they were seen.
SQL_SOURCES = (
    "agents/platform/scripts/session_kv_server.py",
    "agents/platform/scripts/findings_queue.py",
    "agents/platform/scripts/session_retention.py",
    "agents/platform/scripts/credential_proxy.py",
)

# Discovery beyond the roster: every other *.py under the repository whose text
# names an owned table, except under these directories (tests, the bench
# harness and its plant tooling, CI tooling, vendored trees, dependency
# installs) and except these file shapes (unit tests; the patch verifiers under
# deploy/docker/patches, which build fixture schemas of their own).
PYTHON_SUFFIX = ".py"
DISCOVERY_SKIP_DIRS = frozenset(
    {".git", "node_modules", "third_party", "bench", "tests", "hack", "__pycache__", ".venv", "venv", "ROBOT"}
)
DISCOVERY_SKIP_FILE_RES = (re.compile(r"^test_.*\.py$"), re.compile(r"^verify_.*\.py$"))
# A string constant is parsed as a Python program when it has more than one
# line, parses, and imports sqlite3: the admin console's in-pod programs.
EMBEDDED_SOURCE_MODULE = "sqlite3"

# Where the schema comes from: importing this module and calling its init_db()
# on a temporary database, as its own tests do, so the migrations and the
# f-string partial indexes are the ones that run rather than a transcription.
SCRIPTS_DIR = REPO_ROOT / "agents" / "platform" / "scripts"
SCHEMA_MODULE = "session_kv_server"
SCHEMA_INIT_FUNCTION = "init_db"
SCHEMA_DB_PATH_ATTR = "SESSION_KV_DB_PATH"
SCHEMA_DB_FILENAME = "session_kv.db"
DEPS_HINT = "install the platform scripts' imports with: make test-python-deps"

DEFAULT_BASELINE = REPO_ROOT / "scripts" / "sqlite_query_plan_baseline.txt"
BASELINE_COMMENT_PREFIX = "#"
# An entry line is `<path> <statement>`; the path has no spaces.
BASELINE_SITE_SEPARATOR = " "

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

# Hermes's tables, whose DDL is in the image and not in this repository. A
# statement on one is skipped; a statement on any other unknown table is an
# error, since the only other way to reach `no such table` is a typo or a
# rename that missed a statement.
KNOWN_FOREIGN_TABLES = frozenset(
    {
        "sessions", "messages", "state_meta",
        "tasks", "task_events", "task_comments", "task_runs", "task_links",
        "kanban_notify_subs", "kanban_worker_children",
        "executions",
    }
)

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
# A table name in table position, so prose that says "findings" or "incidents"
# does not read as a statement on the table.
NAMES_RE_TEMPLATE = r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+(?:{names})\b"
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

    @property
    def key(self) -> tuple[str, str]:
        return (self.path, self.normalized)


@dataclass(frozen=True)
class Scan:
    table: str
    detail: str


@dataclass
class PlanResult:
    statement: Statement
    kind: str
    # Full scans on unbounded tables; `allowed` holds the ones on bounded tables.
    scans: tuple[Scan, ...] = ()
    allowed: tuple[Scan, ...] = ()
    message: str = ""
    # A dynamic statement that names an owned table: unplanned, so acknowledged.
    on_owned_table: bool = False


@dataclass
class BaselineEntry:
    reason: str
    sites: int = 1


Baseline = dict[tuple[str, str], BaselineEntry]


@dataclass
class Report:
    """Every result, with what needs acknowledging sorted from what has been.

    `known` and `unacknowledged` hold scans on unbounded tables and dynamic
    statements on owned tables, one entry per site; a baseline line covers one
    site, so a second site of a known statement lands in `unacknowledged`.
    """

    results: list[PlanResult] = field(default_factory=list)
    discovered: list[str] = field(default_factory=list)
    allowed_scans: list[tuple[PlanResult, Scan]] = field(default_factory=list)
    known: list[PlanResult] = field(default_factory=list)
    unacknowledged: list[PlanResult] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)

    def of_kind(self, kind: str) -> list[PlanResult]:
        return [r for r in self.results if r.kind == kind]

    @property
    def ok(self) -> bool:
        return not self.unacknowledged and not self.stale and not self.of_kind(KIND_ERROR)


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


def names_re(names: frozenset[str]) -> re.Pattern[str]:
    return re.compile(NAMES_RE_TEMPLATE.format(names="|".join(re.escape(name) for name in sorted(names))))


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


def _imports_module(tree: ast.Module, module: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name == module for alias in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == module:
            return True
    return False


def _embedded_program(text: str) -> ast.Module | None:
    """The string parsed as a Python program, when it is one that uses sqlite3."""
    if "\n" not in text or EMBEDDED_SOURCE_MODULE not in text:
        return None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    return tree if _imports_module(tree, EMBEDDED_SOURCE_MODULE) else None


class _SqlCollector(ast.NodeVisitor):
    def __init__(self, path: str, constants: dict[str, str], line_offset: int = 0) -> None:
        self.path = path
        self.constants = constants
        self.line_offset = line_offset
        self.found: list[Statement] = []

    def visit(self, node: ast.AST) -> None:
        text = render_string(node, self.constants)
        if text is not None:
            line = self.line_offset + getattr(node, "lineno", 0)
            if looks_like_sql(text):
                self.found.append(Statement(self.path, line, text))
            elif isinstance(node, ast.Constant):
                program = _embedded_program(text)
                if program is not None:
                    # The program's first line is the line the string opens on.
                    self.found.extend(_collect(program, self.path, line - 1))
            return
        # NodeVisitor.visit's dispatch, repeated here because this override
        # replaces it: visit_Expr below is where docstrings are dropped.
        getattr(self, VISITOR_METHOD_PREFIX + node.__class__.__name__, self.generic_visit)(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        # A bare string statement is a docstring (or a no-op), not SQL.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)


def _collect(tree: ast.Module, path: str, line_offset: int = 0) -> list[Statement]:
    collector = _SqlCollector(path, module_constants(tree), line_offset)
    collector.visit(tree)
    return collector.found


def collect_statements(path: str, source: str) -> list[Statement]:
    return _collect(ast.parse(source, filename=path), path)


def collect_sources(root: Path = REPO_ROOT, sources: tuple[str, ...] = SQL_SOURCES) -> list[Statement]:
    statements: list[Statement] = []
    for relative in sources:
        file = root / relative
        if not file.is_file():
            raise FileNotFoundError(f"{relative} is on the roster and missing")
        statements.extend(collect_statements(relative, file.read_text(encoding="utf-8")))
    return statements


def discover_sources(owned: frozenset[str], root: Path = REPO_ROOT, roster: tuple[str, ...] = SQL_SOURCES) -> list[str]:
    """Python files off the roster whose text names an owned table in table position, repository-relative.

    A text match is the cheap pre-filter; collect_discovered decides by statement.
    """
    pattern = names_re(owned)
    found: list[str] = []
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(name for name in subdirs if name not in DISCOVERY_SKIP_DIRS)
        for name in sorted(files):
            if not name.endswith(PYTHON_SUFFIX) or any(skip.match(name) for skip in DISCOVERY_SKIP_FILE_RES):
                continue
            file = Path(directory) / name
            relative = file.relative_to(root).as_posix()
            if relative in roster or relative == SELF_PATH:
                continue
            if pattern.search(file.read_text(encoding="utf-8", errors="replace")):
                found.append(relative)
    return found


def collect_discovered(owned: frozenset[str], discovered: list[str], root: Path = REPO_ROOT) -> list[Statement]:
    """The statements in discovered files that name an owned table."""
    pattern = names_re(owned)
    statements: list[Statement] = []
    for relative in discovered:
        for statement in collect_statements(relative, (root / relative).read_text(encoding="utf-8")):
            if pattern.search(statement.sql):
                statements.append(statement)
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


def plan_statement(
    conn: sqlite3.Connection,
    statement: Statement,
    owned: frozenset[str],
    foreign: frozenset[str] = KNOWN_FOREIGN_TABLES,
    bounded: dict[str, str] = BOUNDED_TABLES,
) -> PlanResult:
    keyword = leading_keyword(statement.sql).upper()
    if keyword not in PLANNED_KEYWORDS:
        return PlanResult(statement, KIND_SCHEMA)
    try:
        rows = _explain(conn, statement.sql)
    except sqlite3.Error as exc:
        message = str(exc)
        match = NO_SUCH_TABLE_RE.search(message)
        if match is not None and match.group(1) in foreign:
            return PlanResult(statement, KIND_SKIPPED, message=match.group(1))
        if match is None and any(marker in message for marker in SYNTAX_ERROR_MARKERS):
            on_owned = names_re(owned).search(statement.sql) is not None
            return PlanResult(statement, KIND_DYNAMIC, message=message, on_owned_table=on_owned)
        return PlanResult(statement, KIND_ERROR, message=message)
    scans = full_scans(statement.sql, rows, owned)
    unbounded = tuple(scan for scan in scans if scan.table not in bounded)
    allowed = tuple(scan for scan in scans if scan.table in bounded)
    return PlanResult(statement, KIND_SCAN if scans else KIND_CLEAN, scans=unbounded, allowed=allowed)


def needs_acknowledging(result: PlanResult) -> bool:
    """A scan on an unbounded owned table, or a dynamic statement on an owned one."""
    if result.kind == KIND_SCAN:
        return bool(result.scans)
    return result.kind == KIND_DYNAMIC and result.on_owned_table


def plan_statements(
    conn: sqlite3.Connection,
    statements: list[Statement],
    baseline: Baseline,
    bounded: dict[str, str] = BOUNDED_TABLES,
    foreign: frozenset[str] = KNOWN_FOREIGN_TABLES,
) -> Report:
    owned = owned_tables(conn)
    report = Report()
    seen: dict[tuple[str, str], int] = {}
    for statement in statements:
        result = plan_statement(conn, statement, owned, foreign, bounded)
        report.results.append(result)
        report.allowed_scans.extend((result, scan) for scan in result.allowed)
        if not needs_acknowledging(result):
            continue
        seen[statement.key] = seen.get(statement.key, 0) + 1
        entry = baseline.get(statement.key)
        if entry is not None and seen[statement.key] <= entry.sites:
            report.known.append(result)
        else:
            report.unacknowledged.append(result)
    for key, entry in baseline.items():
        found = seen.get(key, 0)
        if found < entry.sites:
            report.stale.append(
                f"{key[0]} {key[1]}  (covers {entry.sites} site(s), {found} found)"
                if entry.sites > 1 or found
                else f"{key[0]} {key[1]}"
            )
    return report


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------


def parse_baseline(text: str) -> Baseline:
    """(path, statement) -> entry. Comment lines above a line are its reason; a
    repeated line covers one more site."""
    baseline: Baseline = {}
    reason: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            reason = []
            continue
        if line.startswith(BASELINE_COMMENT_PREFIX):
            reason.append(line.lstrip(BASELINE_COMMENT_PREFIX).strip())
            continue
        path, separator, statement = line.partition(BASELINE_SITE_SEPARATOR)
        if not separator or not path.endswith(PYTHON_SUFFIX):
            raise ValueError(f"baseline entry does not open with a .py path: {line}")
        key = (path, statement.strip())
        if key in baseline:
            baseline[key].sites += 1
            continue
        if not reason:
            raise ValueError(f"baseline entry has no reason above it: {line}")
        baseline[key] = BaselineEntry(" ".join(reason))
    return baseline


def load_baseline(path: Path) -> Baseline:
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


def _how(result: PlanResult) -> str:
    if result.kind == KIND_SCAN:
        return SCAN_SEPARATOR.join(scan.detail for scan in result.scans)
    return "dynamic"


def render_report(report: Report, baseline: Baseline) -> str:
    lines: list[str] = []
    planned = [r for r in report.results if r.kind in (KIND_CLEAN, KIND_SCAN)]
    lines.append(
        f"sqlite query plans: {len(report.results)} statements; {len(planned)} planned, "
        f"{len(report.of_kind(KIND_SCHEMA))} schema/transaction, {len(report.of_kind(KIND_DYNAMIC))} dynamic, "
        f"{len(report.of_kind(KIND_SKIPPED))} skipped, {len(report.of_kind(KIND_ERROR))} errors"
    )
    if report.discovered:
        lines.append("")
        lines.append("files off the roster that name an owned table (their statements on those tables are planned)")
        lines.extend(f"  {path}" for path in report.discovered)
    skipped = report.of_kind(KIND_SKIPPED)
    if skipped:
        lines.append("")
        lines.append("skipped: Hermes tables, which this repository defines no DDL for")
        for result in skipped:
            lines.append(f"  {result.statement.where}  table {result.message}  {_clip(result.statement.sql)}")
    dynamic = [r for r in report.of_kind(KIND_DYNAMIC) if not r.on_owned_table]
    if dynamic:
        lines.append("")
        lines.append("dynamic on other tables: not SQL as written, not planned")
        for result in dynamic:
            lines.append(f"  {result.statement.where}  {_clip(result.statement.sql)}")
    if report.allowed_scans:
        lines.append("")
        lines.append("scans on bounded tables (allowed)")
        for result, scan in report.allowed_scans:
            lines.append(f"  {result.statement.where}  {scan.detail}  {_clip(result.statement.sql)}")
    if report.known:
        lines.append("")
        lines.append("in the baseline (known): full scans, and dynamic statements on owned tables")
        for result in report.known:
            lines.append(f"  {result.statement.where}  {_how(result)}  {_clip(result.statement.sql)}")
            lines.append(f"      {baseline[result.statement.key].reason}")
    errors = report.of_kind(KIND_ERROR)
    if errors:
        lines.append("")
        lines.append("ERROR: statements that failed to plan on a table that is neither owned nor Hermes's, or on an owned one")
        for result in errors:
            lines.append(f"  {result.statement.where}  {result.message}")
            lines.append(f"      {_clip(result.statement.sql)}")
    new_scans = [r for r in report.unacknowledged if r.kind == KIND_SCAN]
    if new_scans:
        lines.append("")
        lines.append("FAIL: full scans not in the baseline")
        for result in new_scans:
            lines.append(f"  {result.statement.where}  {_how(result)}")
            lines.append(f"      {result.statement.path} {result.statement.normalized}")
        lines.append("  Add an index, or add the line above to the baseline under a reason naming the issue that owns the fix.")
    new_dynamic = [r for r in report.unacknowledged if r.kind == KIND_DYNAMIC]
    if new_dynamic:
        lines.append("")
        lines.append("FAIL: dynamic statements on owned tables not in the baseline (the planner cannot see them)")
        for result in new_dynamic:
            lines.append(f"  {result.statement.where}  {result.message}")
            lines.append(f"      {result.statement.path} {result.statement.normalized}")
        lines.append("  Give the statement a fixed shape, or add the line above to the baseline under a reason saying why its shape is safe.")
    if report.stale:
        lines.append("")
        lines.append("FAIL: baseline entries with no matching site (remove them, or one line per site that is gone)")
        lines.extend(f"  {entry}" for entry in report.stale)
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
        try:
            conn = build_schema(Path(work))
        except ModuleNotFoundError as exc:
            print(f"sqlite query plans: cannot import {SCHEMA_MODULE} ({exc}); {DEPS_HINT}", file=out)
            return 1
        try:
            owned = owned_tables(conn)
            discovered = collect_discovered(owned, discover_sources(owned))
            statements.extend(discovered)
            report = plan_statements(conn, statements, baseline)
            report.discovered = sorted({statement.path for statement in discovered})
        finally:
            conn.close()
    print(render_report(report, baseline), file=out)
    print(f"({time.monotonic() - started:.1f}s)", file=out)
    return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE, help="known full scans, one line per site")
    args = parser.parse_args(argv)
    return run(args.baseline)


if __name__ == "__main__":
    sys.exit(main())
