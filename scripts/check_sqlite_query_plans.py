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
values; a list can be appended to after its literal, so it does not count), a
constant of another target module named through it (`findings_queue.X`,
resolved against that module's own names), or a name bound exactly once in
the enclosing function or in a function enclosing that one (the innermost
binding wins, as it does when the code runs), by a plain assignment (`a = x`,
or `a, b = x, y` element-wise), to such an expression. Scope follows Python's
rule: a binding counts against the function or module whose own body holds
it, wherever in that body (a module constant rebound inside a top-level `if`
or `try` is bound twice); a nested function, lambda or class is a scope of
its own, so its parameters and locals do not count against the function
around it, and a statement written inside one reads that scope's names
first (a lambda's parameter, not the enclosing function's local of the same
name; a class body's names are read by the statements in that body and by
no method or lambda inside it, which is Python's rule); and a `nonlocal` (or,
for the module, `global`) declaration in any scope nested inside counts as a
binding of the name it declares, since it lets that scope rebind it. A name
bound any other way, or in two places, resolves to nothing, never to a module
constant of the same name. Anything
else (`.format(...)`, `%`, a join over a comprehension, a name assigned in a
branch) is listed as `dynamic` and not judged. A statement handed to a helper
is read at the call site, one hop deep in the same module: a function (at
any depth; `_ids_where_in` and the nested `newest` in the server) whose own
body executes one of its parameters, bare or through
`parameter.format(name=value, ...)`, is an executing helper, and the
argument a call passes in that parameter's place, by position or by keyword,
is a statement of the calling function, resolved where the call is written
by the rules above (a name resolves to the helper visible where the call
is written, innermost scope first, the same way a value does; a helper bound
more than once, called through an attribute, or reached from another
module is not read). The helper's own execute call stays `dynamic`, since
its parameter has no value of its own. When the helper formats its
parameter, each `{name}` field of the statement is filled with the static
value of the keyword the helper passes under that name, or, for one with no
static value, with a single `?` parameter: `IN ({marks})` over a list the
helper builds at run time plans as `IN (?)`, and the planner's choice of
index does not depend on how many values the list carries. A field the
helper does not fill, or one with a conversion or a format spec, leaves the
statement `dynamic`; a field that stood for a name rather than a value does
not prepare, or plans as the scan it would be, and either is loud.

The schema is the real one, not a replay of the DDL: `SCHEMA_SOURCE` names the
server module, the attribute that holds its database path, the function that
builds the schema and the extra DDL constants to run after it. The module is
imported with that path pointed at a temporary file, as its own test does,
`init_db()` builds every table plus the columns its migration loops add, and the
kanban origins side table is created from its constant. A statement whose
leading keyword is DDL, a transaction word or `PRAGMA` is listed as `schema`
and never prepared (some of those take effect at prepare time), except a
`CREATE` that carries `AS SELECT` or `AS WITH` (a table or view built from a
query): that query is planned on its own, from the `SELECT` or `WITH` on.

Every other statement is prepared under `EXPLAIN QUERY PLAN` with a NULL bound
to each parameter. `no such table` naming a table in `FOREIGN_TABLES`, the
tables another process owns and this repository's schema never builds
(Hermes's `state.db` and `kanban.db` tables), lists the statement as
`skipped` with the table named, and not judged; `no such table` naming any
other table is a problem that fails the check, so a table `init_db()` does
not build (a side table a function creates lazily, a misspelt owned name)
earns its exemption in `FOREIGN_TABLES` with its owner beside it, or is
built, rather than passing by being absent. An entry in `FOREIGN_TABLES`
that the schema does build is a problem too. Any other prepare error (a
missing column, a syntax error) is a problem that fails the check. A plan line
that reads a whole table the schema defines is a finding, unless the table is
in `BOUNDED_TABLES`: tables that hold one row, or a daily-pruned or capped
handful, by design. Two plan lines read a whole table: every `SCAN <table>`,
with or without `USING [COVERING] INDEX` (a scan through an index walks the
whole index, so a count, a projection of indexed columns or an `ORDER BY` an
index serves still reads every row; a `LIMIT` is not in the plan line), and
`SEARCH <table> USING AUTOMATIC ... INDEX`, the planner's transient index,
which it builds by reading every row of the table each time the statement
runs. A `SEARCH` on a real index is not a finding. A `SCAN` through a
partial index reads the rows its `WHERE` admits, which is a bound only when
that set is small by design: the indexes in `BOUNDED_INDEXES` are, with the
reason beside each, and a scan through one is listed `bounded`; any other
partial index is a filter, not a bound (the workload-incident index admits
every announced incident, the fold-anchor index every anchor since the last
start), so a scan through it is a finding like any other. An entry in
`BOUNDED_INDEXES` that the schema does not build as a partial index is a
problem that fails the check, so the list cannot outlive the index it names.
The planner names an aliased table by its alias, which the check maps back
from the statement's FROM, JOIN, UPDATE, INTO and comma list words; an alias
the statement binds to two different tables (an outer query and a subquery
reusing one letter) is a problem that fails the check, since the plan line
does not say which was read. A scan of `sqlite_master`, of a constant row, of
a subquery or of a CTE the statement declares is not a table scan, whether the
plan names it by its own name, by the alias the statement gives it, or as
`(subquery-N)`: the plan's own `CO-ROUTINE <name>` and `MATERIALIZE <name>`
lines declare the derived tables, and a `WITH` clause declares the CTEs. A
scan of any other name the statement does not declare is a problem that fails
the check, so an alias shape the check does not read cannot pass as clean. The
plan wording this reads (`SCAN <name>`, not `SCAN TABLE <name>`) arrived in
SQLite 3.36, so an older `sqlite3` library is a problem that fails the check
before any statement is read.

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
import string
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
# Partial indexes whose WHERE admits a set that is bounded by design, so a
# SCAN through one reads that set and no more: `idx_intercepted_events_backlog_pending`
# (`backlog = 1`) holds the drift events waiting for the digest, which drains
# it. The ledger's other partial indexes filter rather than bound (the
# workload-incident index admits every announced incident; the fold-anchor
# index every anchor since the previous start, released one by one only when
# a triage fails), so they are not here, and a scan through one is a finding.
# Each entry must exist as a partial index in the schema, or the check fails.
BOUNDED_INDEXES = frozenset({"idx_intercepted_events_backlog_pending"})
# Tables the targets read that another process owns and `init_db()` never
# builds, so a statement on one cannot be planned here and is listed
# `skipped`: Hermes's `state.db` (`sessions`, `messages`, `state_meta`,
# `executions`) and `kanban.db` (`tasks`, `task_events`, `task_comments`,
# `task_links`, `kanban_notify_subs`, `kanban_worker_children`). A `no such
# table` on any other name is a problem, so a side table a function creates
# lazily, or a misspelt owned table, earns its entry here with its owner, or
# is built, rather than passing by being absent; an entry the schema does
# build is a problem too.
FOREIGN_TABLES = frozenset(
    {
        "sessions",
        "messages",
        "state_meta",
        "executions",
        "tasks",
        "task_events",
        "task_comments",
        "task_links",
        "kanban_notify_subs",
        "kanban_worker_children",
    }
)
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
# A `CREATE` statement built from a query: `CREATE [TEMP] TABLE t AS SELECT
# ...`, `CREATE VIEW v AS WITH ...`. The query is planned from the match on.
CREATE_KEYWORD = "CREATE"
EMBEDDED_QUERY_RE = re.compile(r"\bAS\s+(?P<query>(?:SELECT|WITH)\b)", re.IGNORECASE)
EXPLAIN_PREFIX = "EXPLAIN QUERY PLAN "
# The first SQLite whose plans read `SCAN <name>` rather than `SCAN TABLE
# <name>`; the regexes below read the newer wording only.
MIN_SQLITE_VERSION = (3, 36, 0)
VERSION_SEPARATOR = "."
# How sqlite3 reports the parameter count of a statement prepared with none.
BINDINGS_RE = re.compile(r"The current statement uses (\d+)")
NO_SUCH_TABLE_PREFIX = "no such table: "
# A plan line that reads a table: `SCAN <name>` or `SEARCH <name>`, the name
# possibly followed by `AS <alias>`; then whatever the loop uses. A SCAN reads
# every row, through an index or not; a SEARCH reads the whole table only when
# it uses an automatic index, which the planner builds by reading every row
# (`SEARCH t USING AUTOMATIC COVERING INDEX (col=?)`, or `AUTOMATIC PARTIAL
# COVERING INDEX`).
SCAN_RE = re.compile(r"^(?P<verb>SCAN|SEARCH) (?P<name>\S+)(?: AS \S+)?(?P<rest>.*)$")
SEARCH_VERB = "SEARCH"
AUTOMATIC_INDEX_MARKER = " USING AUTOMATIC "
# The index a SCAN walks (`SCAN t USING INDEX i`, `SCAN t USING COVERING
# INDEX i`); one in BOUNDED_INDEXES bounds the rows read to those its WHERE admits.
SCAN_INDEX_RE = re.compile(r" USING (?:COVERING )?INDEX (?P<index>\S+)")
# `PRAGMA index_list(<table>)`: the index name and whether it is partial.
INDEX_LIST_PRAGMA = "PRAGMA index_list({table})"
INDEX_LIST_NAME_COLUMN = 1
INDEX_LIST_PARTIAL_COLUMN = 4
# The plan lines that declare a derived table, a FROM-clause subquery or a
# CTE the planner runs as a co-routine or materialises, under the name its
# later `SCAN`/`SEARCH` lines use: the alias when the statement gives one,
# else the CTE's name or `(subquery-N)`.
DERIVED_RE = re.compile(r"^(?:CO-ROUTINE|MATERIALIZE) (?P<name>\S+)")
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
# How an executing helper may transform its parameter before executing it:
# `query.format(marks=marks)`, keywords only. Each `{name}` field of the
# caller's statement takes the keyword's static value, or this one `?`
# parameter when the keyword has none (an `IN` list built at run time), which
# plans the way any length of the list does.
FORMAT_METHOD = "format"
FORMAT_FIELD_PARAMETER = "?"
# The detail column of an EXPLAIN QUERY PLAN row.
PLAN_DETAIL_COLUMN = 3
# How a function is named: `Class.method`, `outer.inner`, `outer.<lambda>`
# (a lambda, as Python names it), and `(module)` for a statement outside any
# function.
QUALIFIED_SEPARATOR = "."
LAMBDA_NAME = "<lambda>"
MODULE_LEVEL = "(module)"
# The nodes that open a scope of their own: a binding inside one belongs to
# it, not to the function or module around it. A comprehension is left out on
# purpose: its targets then count against the enclosing scope, which can only
# make a name resolve to nothing, never to the wrong value.
NESTED_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
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
ALIAS_REBOUND_ERROR = (
    "{file}:{line}: {function}: `{plan}` names {name!r}, an alias the statement binds to more than one table; "
    "give each reference its own alias"
)
SQLITE_VERSION_ERROR = (
    "sqlite3 library {found} is older than {minimum}, the first whose plans read `SCAN <name>`; "
    "run the check on a newer SQLite"
)
TARGET_MISSING_ERROR = "{target}: a configured target this check reads is missing; move the entry in {script} with the file"
SCHEMA_IMPORT_ERROR = "{module}: the schema module does not import: {error}"
SCHEMA_ATTR_ERROR = "{module}: the schema module has no attribute {attr}; update {script}"
BOUNDED_INDEX_ERROR = (
    "{script}: BOUNDED_INDEXES names {index!r}, which the schema does not build as a partial index; "
    "drop the entry or build the index"
)
FOREIGN_TABLE_ERROR = (
    "{file}:{line}: {function}: `{statement}` names {table!r}, a table the schema does not build and FOREIGN_TABLES "
    "does not list; build it in the schema, fix the name, or add it to FOREIGN_TABLES in {script} with its owner"
)
FOREIGN_TABLE_BUILT_ERROR = "{script}: FOREIGN_TABLES names {table!r}, which the schema builds; drop the entry"
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
    """The statically resolvable names of one module, and of the other targets it names.

    A sibling's constant is resolved against the sibling's own names (its
    f-string reads its own column tuple, not one of the same name here), so
    each sibling is held as a `_Constants` of its own, one hop deep: a
    sibling's constant that names a third module resolves to nothing.
    """

    def __init__(self, module: ast.Module, siblings: dict[str, ast.Module]) -> None:
        self.names = _module_constants(module)
        self.siblings: dict[str, _Constants] = {name: _Constants(tree, {}) for name, tree in siblings.items()}
        # The module-level functions that execute a parameter; a sibling's are
        # not read (one hop, same module).
        self.helpers: dict[str, _Helper] = _helpers(module, ast.Global, [])


def _module_constants(module: ast.Module) -> dict[str, ast.expr]:
    """Module-level names bound exactly once, by a plain assignment, with the expression assigned.

    A binding anywhere in the module's own body counts (inside a top-level
    `if`, `try`, `for` or `with` too), and so does a `global` declaration in
    any function or class nested in the module, which lets it rebind the name.
    """
    return {name: value for name, value in _bindings(module, ast.Global).items() if value is not None}


def _own_nodes(scope: ast.AST) -> Iterable[ast.AST]:
    """Every node of a scope's own body: the nested scopes are kept as nodes (a `def` binds its name) and not entered."""
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, NESTED_SCOPE_NODES):
            pending.extend(ast.iter_child_nodes(node))


def _bindings(scope: ast.AST, declaration: type[ast.Global] | type[ast.Nonlocal]) -> dict[str, ast.expr | None]:
    """Every name a scope binds: the expression, when bound exactly once by a plain assignment, else None.

    Every binding in the scope's own body counts against the name: a store to
    it (an assignment, an unpacking, a loop or `with` target, a walrus, an
    augmented assignment), a parameter, an `except ... as`, an `import ...
    as`, a `global` or `nonlocal`, a `match` capture, or a nested `def` or
    `class`, whose body is a scope of its own and is not entered. A
    `declaration` (`nonlocal` for a function, `global` for the module) in any
    scope nested inside, however deep, counts as a binding here too, since it
    lets that scope rebind the name. A name bound any other way, or twice, is
    in the map with None, so it resolves to nothing rather than to the module
    constant of the same name.
    """
    counts, values = _binding_counts(scope, declaration)
    return {name: (values.get(name) if count == 1 else None) for name, count in counts.items()}


def _binding_counts(
    scope: ast.AST, declaration: type[ast.Global] | type[ast.Nonlocal]
) -> tuple[dict[str, int], dict[str, ast.expr]]:
    """How many times a scope binds each name, as `_bindings` counts, and the expression of each plain assignment."""
    counts: dict[str, int] = defaultdict(int)
    values: dict[str, ast.expr] = {}
    for node in _own_nodes(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name, value in _assigned(node):
                values[name] = value
        for name in _bound_names(node):
            counts[name] += 1
        if isinstance(node, NESTED_SCOPE_NODES):
            for inner in ast.walk(node):
                if isinstance(inner, declaration):
                    for name in inner.names:
                        counts[name] += 1
    return counts, values


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


def _local_constants(scope: ast.AST) -> dict[str, ast.expr | None]:
    """Every name a function, lambda or class body binds, as `_bindings` counts them.

    A `nonlocal` in a scope nested inside is a binding here. For a class
    body that over-counts (a method's `nonlocal` names an enclosing
    function's variable, never a class attribute), which can only make a
    class-body name resolve to nothing, never to the wrong value.
    """
    return _bindings(scope, ast.Nonlocal)


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


# The functions enclosing an expression, outermost first, each as the map
# `_local_constants` builds; empty at module level.
Scopes = list[dict[str, ast.expr | None]]


def _lookup(name: str, scopes: Scopes) -> tuple[ast.expr | None, Scopes] | None:
    """The binding of a name in the innermost scope that binds it, with the scopes its expression was written in.

    A nested function reads an enclosing function's local as a closure
    variable, so the search walks outward; the expression it finds is then
    resolved against the scopes up to and including the one that bound it,
    which is where its own names mean what they meant when it was written.
    None when no scope binds the name, so the caller tries the module.
    """
    for index in range(len(scopes) - 1, -1, -1):
        if name in scopes[index]:
            return scopes[index][name], scopes[: index + 1]
    return None


@dataclass(frozen=True)
class _Helper:
    """A function whose own body executes one of its parameters, so a call's argument there is a statement."""

    # The executed parameter, and its position among the positional
    # parameters (None for a keyword-only one).
    parameter: str
    position: int | None
    # The keywords of the `parameter.format(...)` the helper executes, or
    # None when it executes the parameter bare.
    format_keywords: tuple[ast.keyword, ...] | None
    # The scopes the helper's body reads, outermost first, its own last: where
    # the format keywords' values are resolved.
    scopes: Scopes


@dataclass
class _Scope:
    """One scope the collector is inside: its bindings, whether it is a class body, and the helpers it defines."""

    names: dict[str, ast.expr | None]
    is_class: bool
    helpers: dict[str, _Helper]


def _own_defs(scope: ast.AST, declaration: type[ast.Global] | type[ast.Nonlocal]) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """The functions a scope's own body defines under a name the scope binds exactly once."""
    counts, _ = _binding_counts(scope, declaration)
    return {
        node.name: node
        for node in _own_nodes(scope)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and counts.get(node.name) == 1
    }


def _executed_parameter(function: ast.FunctionDef | ast.AsyncFunctionDef, scopes: Scopes) -> _Helper | None:
    """The helper a function is when its own body executes one of its parameters, bare or through `.format(name=...)`.

    The parameter must be bound once (the parameter itself) so that what the
    helper executes is what the caller passed; a `.format` with positional
    arguments or `**kwargs` is not read, and the function is then no helper.
    """
    positional = [arg.arg for arg in (*function.args.posonlyargs, *function.args.args)]
    keyword_only = [arg.arg for arg in function.args.kwonlyargs]
    counts, _ = _binding_counts(function, ast.Nonlocal)
    for node in _own_nodes(function):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in EXECUTE_METHODS
            and node.args
        ):
            continue
        executed = node.args[0]
        keywords: tuple[ast.keyword, ...] | None = None
        if (
            isinstance(executed, ast.Call)
            and isinstance(executed.func, ast.Attribute)
            and executed.func.attr == FORMAT_METHOD
            and not executed.args
            and all(keyword.arg is not None for keyword in executed.keywords)
        ):
            keywords = tuple(executed.keywords)
            executed = executed.func.value
        if not isinstance(executed, ast.Name) or counts.get(executed.id) != 1:
            continue
        if executed.id in positional:
            return _Helper(executed.id, positional.index(executed.id), keywords, scopes)
        if executed.id in keyword_only:
            return _Helper(executed.id, None, keywords, scopes)
    return None


def _helpers(scope: ast.AST, declaration: type[ast.Global] | type[ast.Nonlocal], scopes: Scopes) -> dict[str, _Helper]:
    """name -> helper, for every function the scope's own body defines that executes a parameter.

    `scopes` are the maps visible where the scope's body is written; each
    helper's own locals go innermost, since its format keywords are its own.
    """
    found: dict[str, _Helper] = {}
    for name, function in _own_defs(scope, declaration).items():
        helper = _executed_parameter(function, [*scopes, _local_constants(function)])
        if helper is not None:
            found[name] = helper
    return found


def _passed(call: ast.Call, helper: _Helper) -> ast.expr | None:
    """The argument a call passes in the helper's executed parameter's place, by keyword or by position, or None."""
    for keyword in call.keywords:
        if keyword.arg == helper.parameter:
            return keyword.value
    if helper.position is None or helper.position >= len(call.args):
        return None
    if any(isinstance(arg, ast.Starred) for arg in call.args[: helper.position + 1]):
        return None
    return call.args[helper.position]


def _formatted(sql: str, helper: _Helper, constants: _Constants) -> str | None:
    """The caller's statement with each `{name}` field filled as the helper's `.format(...)` fills it, or None.

    A keyword with a static value fills its field with that value; one with
    none fills it with a single `?` parameter, so an `IN` list the helper
    builds at run time plans as `IN (?)`. A field the helper does not fill,
    or one with a conversion or a format spec, is not read: None, dynamic.
    """
    if helper.format_keywords is None:
        return sql
    values = {str(keyword.arg): _resolve(keyword.value, constants, helper.scopes) for keyword in helper.format_keywords}
    try:
        fields = list(string.Formatter().parse(sql))
    except ValueError:
        return None
    parts: list[str] = []
    for literal, field, spec, conversion in fields:
        parts.append(literal)
        if field is None:
            continue
        if spec or conversion is not None or field not in values:
            return None
        value = values[field]
        parts.append(FORMAT_FIELD_PARAMETER if value is None else str(value))
    return "".join(parts)


def _resolve(expr: ast.expr, constants: _Constants, scopes: Scopes, depth: int = 0) -> str | int | float | None:
    """The static value of an expression, or None when it has none."""
    if depth > RESOLVE_DEPTH:
        return None
    if isinstance(expr, ast.Constant) and isinstance(expr.value, (str, int, float)) and not isinstance(expr.value, bool):
        return expr.value
    if isinstance(expr, ast.Name):
        found = _lookup(expr.id, scopes)
        if found is not None:
            bound, visible = found
            return None if bound is None else _resolve(bound, constants, visible, depth + 1)
        source = constants.names.get(expr.id)
        return None if source is None else _resolve(source, constants, [], depth + 1)
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        sibling = constants.siblings.get(expr.value.id)
        source = None if sibling is None else sibling.names.get(expr.attr)
        return None if source is None or sibling is None else _resolve(source, sibling, [], depth + 1)
    if isinstance(expr, ast.JoinedStr):
        parts: list[str] = []
        for value in expr.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            elif isinstance(value, ast.FormattedValue) and value.format_spec is None and value.conversion == -1:
                inner = _resolve(value.value, constants, scopes, depth + 1)
                if inner is None:
                    return None
                parts.append(str(inner))
            else:
                return None
        return "".join(parts)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == STR_CALL:
        if len(expr.args) != 1 or expr.keywords:
            return None
        inner = _resolve(expr.args[0], constants, scopes, depth + 1)
        return None if inner is None else str(inner)
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == JOIN_METHOD:
        if len(expr.args) != 1 or expr.keywords:
            return None
        separator = _resolve(expr.func.value, constants, scopes, depth + 1)
        items = _sequence_items(expr.args[0], constants, scopes, depth + 1)
        if not isinstance(separator, str) or items is None:
            return None
        parts = [_resolve(item, constants, scopes, depth + 1) for item in items]
        if not all(isinstance(part, str) for part in parts):
            return None
        return separator.join(parts)  # type: ignore[arg-type]
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _resolve(expr.left, constants, scopes, depth + 1)
        right = _resolve(expr.right, constants, scopes, depth + 1)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        return None
    return None


def _sequence_items(expr: ast.expr, constants: _Constants, scopes: Scopes, depth: int) -> list[ast.expr] | None:
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
        found = _lookup(expr.id, scopes)
        if found is not None:
            bound, visible = found
            return None if bound is None else _sequence_items(bound, constants, visible, depth + 1)
        source = constants.names.get(expr.id)
        return None if source is None else _sequence_items(source, constants, [], depth + 1)
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        sibling = constants.siblings.get(expr.value.id)
        source = None if sibling is None else sibling.names.get(expr.attr)
        return None if source is None or sibling is None else _sequence_items(source, sibling, [], depth + 1)
    return None


class _Collector(ast.NodeVisitor):
    """Every execute call in a module, named by the function it sits in."""

    def __init__(self, shown: str, constants: _Constants) -> None:
        self.shown = shown
        self.constants = constants
        self.stack: list[str] = []
        # The enclosing scopes, outermost first, each with whether it is a
        # class body. A function or lambda reads every enclosing function's
        # names and helpers; a class body's are read by the statements
        # written in that body and by nothing nested in it, since a method or
        # a lambda inside a class does not see what its class body binds.
        self.scopes: list[_Scope] = []
        self.statements: list[Statement] = []

    def _visible(self) -> list[_Scope]:
        """The scopes a statement written here reads: the enclosing functions, and the class body when it is innermost."""
        visible = [scope for scope in self.scopes[:-1] if not scope.is_class]
        if self.scopes:
            visible.append(self.scopes[-1])
        return visible

    @property
    def locals(self) -> Scopes:
        return [scope.names for scope in self._visible()]

    def _helper(self, name: str) -> _Helper | None:
        """The executing helper a bare name means where the call is written: the innermost scope defining it, else the module's."""
        for scope in reversed(self._visible()):
            if name in scope.helpers:
                return scope.helpers[name]
        return self.constants.helpers.get(name)

    def _scoped(self, node: ast.AST, name: str, is_class: bool = False) -> None:
        self.stack.append(name)
        scope = _Scope(_local_constants(node), is_class, {})
        self.scopes.append(scope)
        # A helper defined here reads this scope's names and the enclosing
        # ones, so its map is built with this scope already on the stack.
        scope.helpers = _helpers(node, ast.Nonlocal, self.locals)
        self.generic_visit(node)
        self.scopes.pop()
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scoped(node, node.name, is_class=True)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scoped(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._scoped(node, LAMBDA_NAME)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in EXECUTE_METHODS and node.args:
            self._record(node, node.args[0], None)
        elif isinstance(node.func, ast.Name):
            helper = self._helper(node.func.id)
            argument = None if helper is None else _passed(node, helper)
            if helper is not None and argument is not None:
                self._record(node, argument, helper)
        self.generic_visit(node)

    def _record(self, node: ast.Call, argument: ast.expr, helper: _Helper | None) -> None:
        """One statement: an execute call's first argument, or the argument a call hands an executing helper."""
        value = _resolve(argument, self.constants, self.locals)
        sql = value if isinstance(value, str) else None
        if sql is not None and helper is not None:
            sql = _formatted(sql, helper, self.constants)
        function = QUALIFIED_SEPARATOR.join(self.stack) or MODULE_LEVEL
        self.statements.append(Statement(self.shown, node.lineno, function, sql))


def scan_statements(path: Path, root: Path, trees: dict[str, ast.Module]) -> list[Statement]:
    """Every execute call in one module, with its statement where it resolves.

    `trees` holds every target's parsed module under its stem, this one's
    included, so nothing is read or parsed twice; the others are the siblings
    whose constants a statement may name.
    """
    shown = path.relative_to(root).as_posix()
    module = trees[path.stem]
    others = {name: tree for name, tree in trees.items() if name != path.stem}
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
    trees = {path.stem: ast.parse(path.read_text(encoding="utf-8"), filename=path.relative_to(root).as_posix()) for path in paths}
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
    """The table a plan line reads in full, or None.

    Every `SCAN` reads every row, through an index (the whole index) or not;
    so does a `SEARCH` through an automatic index, whatever its verb, because
    the planner builds that index by reading every row each time the
    statement runs. A `SEARCH` on a real index reads what the index finds.
    """
    match = SCAN_RE.match(plan)
    if match is None:
        return None
    rest = match.group("rest") + " "
    if AUTOMATIC_INDEX_MARKER in rest:
        return match.group("name")
    if match.group("verb") == SEARCH_VERB:
        return None
    return match.group("name")


def scan_index(plan: str) -> str | None:
    """The index a `SCAN` line walks (`SCAN t USING [COVERING] INDEX i`), or None for a table scan or a `SEARCH`."""
    match = SCAN_RE.match(plan)
    if match is None or match.group("verb") == SEARCH_VERB:
        return None
    index = SCAN_INDEX_RE.search(match.group("rest"))
    return None if index is None else index.group("index")


def partial_indexes(conn: sqlite3.Connection, tables: Iterable[str]) -> frozenset[str]:
    """The names of the partial indexes (those with a WHERE) over the given tables, which BOUNDED_INDEXES must be drawn from."""
    names: set[str] = set()
    for table in tables:
        for row in conn.execute(INDEX_LIST_PRAGMA.format(table=table)).fetchall():
            if row[INDEX_LIST_PARTIAL_COLUMN]:
                names.add(row[INDEX_LIST_NAME_COLUMN])
    return frozenset(names)


def derived_names(plans: Iterable[str]) -> frozenset[str]:
    """The derived tables a plan declares (`CO-ROUTINE <name>`, `MATERIALIZE <name>`), which no later scan line reads as a table."""
    found: set[str] = set()
    for plan in plans:
        match = DERIVED_RE.match(plan)
        if match is not None:
            found.add(match.group("name"))
    return frozenset(found)


def table_aliases(sql: str, tables: frozenset[str]) -> dict[str, str | None]:
    """alias -> table for every aliased reference in the statement to one of `tables` (schema tables, CTEs and derived tables).

    An alias the statement binds to two different tables (one letter reused
    by an outer query and a subquery) maps to None: the planner prints the
    alias alone, so a scan of it cannot be placed, and `judge` reports it
    rather than guessing.
    """
    found: dict[str, str | None] = {}
    for match in ALIAS_RE.finditer(sql):
        table, alias = match.group("table"), match.group("alias")
        if alias is None or table not in tables:
            continue
        if alias not in found:
            found[alias] = table
        elif found[alias] != table:
            found[alias] = None
    return found


def cte_names(sql: str) -> frozenset[str]:
    """The common table expressions the statement declares."""
    return frozenset(match.group("name") for match in CTE_RE.finditer(sql))


def _non_table_scan(name: str, derived: frozenset[str]) -> bool:
    """Whether a scan name is a constant row, an unflattened subquery, a CTE or a derived table the plan declares, not a table."""
    return name in NON_TABLE_SCAN_NAMES or name.isdecimal() or name.startswith(SUBQUERY_SCAN_PREFIXES) or name in derived


def planned_text(sql: str) -> str | None:
    """The text to prepare under EXPLAIN QUERY PLAN, or None for a schema, transaction or PRAGMA statement.

    A `CREATE ... AS SELECT` or `CREATE ... AS WITH` is DDL that carries a
    query; the query is what reads a table, so it is planned on its own, from
    the `SELECT` or `WITH` on, and the `CREATE` never runs.
    """
    keyword = leading_keyword(sql)
    if keyword == CREATE_KEYWORD:
        embedded = EMBEDDED_QUERY_RE.search(sql)
        if embedded is not None:
            return sql[embedded.start("query") :]
    return None if keyword in SCHEMA_KEYWORDS else sql


def judge(statements: Iterable[Statement], conn: sqlite3.Connection) -> tuple[list[Verdict], list[str]]:
    """Every statement classified, and the statements that do not prepare for a reason other than a foreign table."""
    tables = schema_tables(conn)
    partial = partial_indexes(conn, tables)
    verdicts: list[Verdict] = []
    # A bounded index the schema no longer builds as a partial index would
    # otherwise sit in the list forever, vouching for nothing.
    problems: list[str] = [
        BOUNDED_INDEX_ERROR.format(script=SCRIPT_NAME, index=index) for index in sorted(BOUNDED_INDEXES - partial)
    ]
    # A foreign table the schema builds is an owned table hidden from the
    # check under the wrong list.
    problems.extend(FOREIGN_TABLE_BUILT_ERROR.format(script=SCRIPT_NAME, table=table) for table in sorted(FOREIGN_TABLES & tables))
    for statement in statements:
        if statement.sql is None:
            verdicts.append(Verdict(statement, KIND_DYNAMIC))
            continue
        text = planned_text(statement.sql)
        if text is None:
            verdicts.append(Verdict(statement, KIND_SCHEMA))
            continue
        try:
            plans = explain(conn, text)
        except sqlite3.Error as exc:
            message = str(exc)
            if message.startswith(NO_SUCH_TABLE_PREFIX):
                table = message[len(NO_SUCH_TABLE_PREFIX) :]
                if table in FOREIGN_TABLES:
                    verdicts.append(Verdict(statement, KIND_SKIPPED, table=table))
                    continue
                # A table neither built nor listed as foreign: a lazily
                # created side table or a misspelt owned name, not a Hermes
                # table, so it is reported rather than skipped.
                verdicts.append(Verdict(statement, KIND_ERROR, table=table, plan=message))
                problems.append(
                    FOREIGN_TABLE_ERROR.format(
                        file=statement.file,
                        line=statement.line,
                        function=statement.function,
                        statement=statement.preview,
                        table=table,
                        script=SCRIPT_NAME,
                    )
                )
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
        # The CTEs the statement declares and the derived tables the plan
        # declares are names a scan line may use, directly or through an
        # alias, and none of them is a table.
        derived = cte_names(statement.sql) | derived_names(plans)
        aliases = table_aliases(statement.sql, tables | derived)
        for plan in plans:
            name = scanned_table(plan)
            if name is None or name in SCHEMA_TABLES:
                continue
            table = name if name in tables else aliases.get(name)
            if table in derived:
                continue
            if table is None:
                if name in aliases:
                    # Bound to two different tables: the plan line does not
                    # say which one it read, so it is reported, not guessed.
                    judged += 1
                    verdicts.append(Verdict(statement, KIND_ERROR, plan=plan))
                    problems.append(
                        ALIAS_REBOUND_ERROR.format(
                            file=statement.file, line=statement.line, function=statement.function, plan=plan, name=name
                        )
                    )
                    continue
                if _non_table_scan(name, derived):
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
            # A scan through a partial index reads the rows its WHERE admits,
            # a bound only for the indexes BOUNDED_INDEXES vouches for.
            bounded = table in BOUNDED_TABLES or scan_index(plan) in BOUNDED_INDEXES
            verdicts.append(Verdict(statement, KIND_BOUNDED if bounded else KIND_FINDING, table=table, plan=plan))
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


def _version_text(version: tuple[int, ...]) -> str:
    return VERSION_SEPARATOR.join(str(part) for part in version)


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
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        print(SQLITE_VERSION_ERROR.format(found=sqlite3.sqlite_version, minimum=_version_text(MIN_SQLITE_VERSION)), file=err)
        print(SUMMARY_FAIL.format(findings=0, problems=1), file=err)
        return EXIT_FAIL
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
