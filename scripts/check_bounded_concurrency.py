#!/usr/bin/env python3
"""Fail when a request handler can start a thread or goroutine with no bound on how many.

The credential proxy (the broker), the session KV server and the a2a auth
callout each serve requests from a fleet whose size they do not control. A
thread started per request, or a goroutine started per message, grows with the
fleet until the process hits its thread or descriptor limit (#4331, #4332).
Nothing in the unit tests sees that shape, because every test runs one request.
This check reads the source instead and fails a pull request that adds one.

Python (`PYTHON_TARGETS`), by AST. The entry points are request handlers: every
method of a class whose base name ends in `RequestHandler`, and every function
carrying a FastAPI route decorator (`@app.get(...)`, `@router.post(...)`). A
call graph is built per module from direct calls: a bare `name(...)`, a class
instantiation (an edge to its `__init__`), `self.name(...)` or `cls.name(...)`
to the same class's method, and a function handed on as a value where the
handing is itself how a request's work continues: the first argument of
`.add_task(...)` (Starlette's `BackgroundTasks`) or `.submit(...)` (an executor
or a queue), a FastAPI dependency (`Depends(fn)`, in a route's `dependencies=`
or a parameter default, which runs before every request the route serves), and
the callable a spawn is given (`target=`, a `Timer`'s function,
`start_new_thread`'s). A call through any other receiver (a module-level
singleton, a callback stored in one call and invoked in another) is not
followed; that is the check's blind spot, named here so a reviewer knows it. A
spawn is a call that starts one or more threads inside the function: a
`threading.Thread(...)` or `threading.Timer(...)`, `_thread.start_new_thread(...)`,
a `ThreadPoolExecutor(...)` built inside the function (its workers are the
request's, whatever `max_workers` says; one built at module level is not inside
any function and is the bounded pool the rule asks for), and an instance of a
class in the module that subclasses `Thread` or `Timer`. A spawn in a function a
handler reaches is a finding unless its shape is bounded:

* a singleton guard: the spawn sits in the body of an `if` whose test reads a
  thread's `.is_alive()` (its `else` branch is not covered), or is the first
  spawn after such an `if` whose body returns, so at most one such thread runs
  at a time; a second spawn after the early return is not under the guard, and
  neither is a spawn with a loop (`for`, `while`, a comprehension) opened
  between the guard and it, since the guard was read once and the loop spawns
  per row;
* startup: the function is `main`.

A loop over `range(n)` is not a bound, whatever `n` is: a handler that starts a
constant number of threads per request still starts a number that grows with
the callers, the same shape as a `ThreadPoolExecutor` built per request. A
module-level pool built once by a function no handler reaches is not flagged,
since only reachability flags a spawn.

Go (`GO_TARGET_DIRS`), by text, since the callout has no request-handler marker
to anchor a graph on. Comments and string literals are stripped from the whole
file first, a raw string or block comment spanning lines included, and the rest
is read as code. Every `go` statement in a non-test file, whatever call
expression follows the keyword (`go f(m)`, `go s.handlers[i](m)`, `go (&w{}).run()`),
at the start of a line or after a `{`, `;` or `:` on it, each one on a line
counted on its own, is a finding
unless it sits in a startup function, or a semaphore gates it in the same block
before the statement (a send on a channel whose name starts with `sem` or
carries `Sem`, or an `Acquire(...)`, written between the `{` that opens the
innermost block holding the statement and the statement itself, so a send
above the loop the statement is in bounds nothing). The startup functions are
`main` and, transitively, every function a startup function calls by name
outside its function literals, across all the scanned files; a name shared by a
startup function and a per-request one is read as startup, which is the text
scan's blind spot. A startup function's bound stops at a function literal: a
`go` statement inside a closure the function builds (`mux.HandleFunc(path,
func(w, r) { go ... })`, a subscription callback), and whatever that closure
calls, is not startup, since the closure runs when its caller decides, which
for a handler is once per request. A function is read from the `{` that opens
its body on the declaration line (so a one-line function, `func f() { go g() }`,
is read like any other and a function type in the signature opens nothing). A
`func(` opens a literal when the `{` that
follows its parameter list comes before a `;` or the end of that line (so a
function type, `var cb func()` or `map[string]func()`, opens none); the body is
the brace pair that `{` starts, and the literal is found by brace nesting, so a
goroutine a startup function starts inside another of its own closures
(`go func() { go work() }()`) is read the same way and is pinned rather than
bounded. An `errgroup` with `SetLimit(...)` bounds its own `Go(...)` calls,
which are not `go` statements and are not read; it does not bound a `go`
statement near it.

``scripts/bounded_concurrency_allowlist.txt``: one line per function that
carried a finding when the check arrived, `<file> <function> <count>`, with the
issue that owns the fix, or the bound the check cannot see, in a comment above
it. The list only shrinks: a count above the entry fails (a rise), a count
below fails until the entry is lowered or removed (a drop), an entry whose
function has no finding left fails (stale), an entry naming a file the check
does not read fails, and a line that is not an entry fails. A function with no
entry is held at zero. A configured target that is missing is a failure too, so
a moved file cannot turn the check green by leaving the scan.

Exit 0 when every finding is pinned exactly; 1 otherwise. `--list` prints every
spawn the scan saw with its classification, pinned or not.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TextIO

REPO_ROOT = Path(__file__).resolve().parent.parent

# The Python modules the check reads, repository-relative: the broker and the KV server.
PYTHON_TARGETS: tuple[str, ...] = (
    "agents/platform/scripts/credential_proxy.py",
    "agents/platform/scripts/session_kv_server.py",
)
# The Go directories the check reads, repository-relative: the auth callout's
# library and its command. Every `.go` file under them that is not a test.
GO_TARGET_DIRS: tuple[str, ...] = ("a2a/authcallout", "a2a/cmd/authcallout")
GO_SUFFIX = ".go"
GO_TEST_SUFFIX = "_test.go"
GO_TESTDATA_DIR = "testdata"

# The shrinking allowlist: `<file> <function> <count>` per line, `#` comments and
# blank lines ignored.
ALLOWLIST = REPO_ROOT / "scripts" / "bounded_concurrency_allowlist.txt"
ALLOWLIST_FIELDS = 3
COMMENT_PREFIX = "#"

# Python entry points: a class whose base name ends like this is a request
# handler (BaseHTTPRequestHandler, StreamRequestHandler), and every method of it
# runs per request; a function decorated with one of these FastAPI attributes
# is a route.
REQUEST_HANDLER_BASE_SUFFIX = "RequestHandler"
ROUTE_DECORATOR_ATTRS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "api_route", "websocket"})
# The callee names that start one or more threads. `Thread` and `Timer` cover
# `threading.Thread(...)` and a bare import; `start_new_thread` is the `_thread`
# primitive; a `ThreadPoolExecutor` built inside a function starts its workers
# for that call. A class in the module whose base is one of THREAD_BASES is a
# spawn too when instantiated.
THREAD_SPAWN_CALLEES = frozenset({"Thread", "Timer", "start_new_thread", "ThreadPoolExecutor"})
THREAD_BASES = frozenset({"Thread", "Timer"})
# Calls that hand a function on as a value and run it as the request's work:
# the callee name, and the position of the function among the positional
# arguments. `add_task` is Starlette's `BackgroundTasks`, `submit` an executor
# or a queue, `Depends(fn)` a FastAPI dependency the route runs before its body,
# `Timer(interval, function)` and `start_new_thread(function, args)` the
# spawns' own callables.
CALLBACK_POSITIONS: dict[str, int] = {"add_task": 0, "submit": 0, "Depends": 0, "start_new_thread": 0, "Timer": 1}
# A loop between a liveness guard and a spawn runs the spawn per row, so the
# guard bounds nothing under it; comprehensions are loops too.
LOOP_NODES = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
# Keywords that hand a function on the same way: `Thread(target=...)`,
# `Timer(function=...)`.
CALLBACK_KEYWORDS = frozenset({"target", "function"})
# The bounded shapes, and the words the report uses for them.
SINGLETON_GUARD_ATTR = "is_alive"
STARTUP_FUNCTION = "main"
SELF_RECEIVERS = frozenset({"self", "cls"})
BOUND_SINGLETON = "singleton guard (is_alive)"
BOUND_STARTUP = "startup (main)"
BOUND_GO_STARTUP = "startup (reached from main)"
BOUND_GO_SEMAPHORE = "a semaphore before the statement, in its block"
CONSTRUCTOR = "__init__"
QUALIFIED_SEPARATOR = "."

# Go, by text, on lines with comments and strings removed. A spawn is a `go`
# statement at the start of a line or after a `{`, `;` or `:` on it, whatever
# call expression follows the keyword (group 1 is the statement, so its column
# is known; `go` is a keyword, so after stripping nothing else reads `go `
# there); a function starts at column 0 with `func`, optionally with a
# receiver, its body opens at the first `{` on that line outside parentheses
# and brackets that no `struct` or `interface` precedes, and it ends on the
# declaration line when that line's braces balance, else at the first `}` at
# column 0 (gofmt's shapes, which `go vet` in CI holds).
GO_SPAWN_RE = re.compile(r"(?:^|[{;:])\s*(go\s+(?=\S))")
GO_FUNC_RE = re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")
GO_FUNC_END = "}"
GO_NEST_OPEN = frozenset({"(", "["})
GO_NEST_CLOSE = frozenset({")", "]"})
GO_TYPE_BLOCK_RE = re.compile(r"\b(?:struct|interface)\s*$")
# A semaphore: a send on a channel named for one (`sem`, `semaphore`, `connSem`;
# not `assembly` or `resemble`), or an `Acquire(` call.
GO_SEMAPHORE_RE = re.compile(r"\b(?:sem\w*|\w*Sem\w*)\s*<-|\.Acquire\(")
GO_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\(")
# A function literal opens where the `{` after a `func(`'s parameter list comes
# before a `;` or the end of that list's line; its body is the brace pair that
# `{` starts.
GO_FUNC_LITERAL = "func("
GO_PAREN_OPEN = "("
GO_PAREN_CLOSE = ")"
GO_STATEMENT_END = ";"
GO_LINE_COMMENT = "//"
GO_BLOCK_COMMENT_OPEN = "/*"
GO_BLOCK_COMMENT_CLOSE = "*/"
GO_RAW_QUOTE = "`"
GO_QUOTES = frozenset({'"', "'"})
GO_ESCAPE = "\\"
GO_BRACE_OPEN = "{"
GO_BRACE_CLOSE = "}"
GO_NO_GRAPH = "(every function; Go has no call graph here)"
GO_NO_FUNCTION = "(outside any function)"
KIND_THREAD = "thread"
KIND_GOROUTINE = "goroutine"

# How the report reads.
FINDING_LINE = "{file}:{line}: {function} starts a {kind} per request, reachable from {handlers}; bound it or pin it in {allowlist}"
LIST_LINE = "{file}:{line}: {function} {kind} reachable_from={handlers} bounded_by={bounded}"
TARGET_MISSING_ERROR = "{target}: a configured target this check reads is missing; move the entry in {script} with the file"
GO_DIR_EMPTY_ERROR = "{target}: a configured Go directory holds no non-test .go file; move the entry in {script} with the package"
ALLOWLIST_MALFORMED_ERROR = "{allowlist}:{line}: expected `<file> <function> <count>` with a count above zero, got {text!r}"
ALLOWLIST_DUPLICATE_ERROR = "{allowlist}:{line}: {file} {function} already has an entry at line {first}"
ALLOWLIST_UNSCANNED_ERROR = "{allowlist}:{line}: {file} is not a file this check reads; remove the entry"
ALLOWLIST_STALE_ERROR = (
    "{allowlist}:{line}: {file} {function} has no unbounded per-request spawn left; remove the entry"
)
ALLOWLIST_RISE_ERROR = (
    "{file}: {function} has {found} unbounded per-request spawn(s), the entry at {allowlist}:{line} allows {allowed}; "
    "bound the new one"
)
ALLOWLIST_DROP_ERROR = (
    "{file}: {function} has {found} unbounded per-request spawn(s), the entry at {allowlist}:{line} allows {allowed}; "
    "lower the entry to {found}"
)
SUMMARY_FAIL = "check_bounded_concurrency: {findings} finding(s) not pinned and {problems} problem(s)"
SUMMARY_OK = "check_bounded_concurrency: {spawns} spawn(s) read across {files} file(s), none unbounded beyond the {entries} allowlist entr(y/ies)"
SCRIPT_NAME = "scripts/check_bounded_concurrency.py"
EXIT_OK = 0
EXIT_FAIL = 1


@dataclass(frozen=True)
class Spawn:
    """One thread or goroutine start the scan saw."""

    file: str
    line: int
    function: str
    kind: str
    # The handlers whose code reaches the function; empty when none does.
    reachable_from: tuple[str, ...]
    # The bounded shape the spawn sits in, or None when nothing bounds it.
    bounded_by: str | None

    @property
    def flagged(self) -> bool:
        return bool(self.reachable_from) and self.bounded_by is None


@dataclass(frozen=True)
class AllowlistEntry:
    """One pinned (file, function) count and the allowlist line that carries it."""

    file: str
    function: str
    count: int
    line: int


@dataclass
class _Unit:
    """One function or method of a Python module, with what it calls and spawns."""

    name: str
    class_name: str | None
    entry_point: bool
    calls: set[str]
    self_calls: set[str]
    spawns: list[tuple[int, str | None]]


@dataclass(frozen=True)
class _ModuleFacts:
    """What a unit's classification needs from the module around it."""

    # Classes of the module that subclass Thread or Timer; instantiating one is a spawn.
    thread_classes: frozenset[str]


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _base_names(node: ast.ClassDef) -> list[str]:
    names = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _is_route_decorator(decorator: ast.expr) -> bool:
    return (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in ROUTE_DECORATOR_ATTRS
    )


def _is_request_handler_class(node: ast.ClassDef) -> bool:
    return any(name.endswith(REQUEST_HANDLER_BASE_SUFFIX) for name in _base_names(node))


def _handed_on(call: ast.Call, callee: str) -> list[ast.expr]:
    """The function values a call hands on to run as the request's work."""
    values: list[ast.expr] = []
    position = CALLBACK_POSITIONS.get(callee)
    if position is not None and len(call.args) > position:
        values.append(call.args[position])
    values.extend(kw.value for kw in call.keywords if kw.arg in CALLBACK_KEYWORDS)
    return values


def _reads_liveness(test: ast.expr) -> bool:
    return any(isinstance(n, ast.Call) and _callee_name(n) == SINGLETON_GUARD_ATTR for n in ast.walk(test))


def _returns(body: list[ast.stmt]) -> bool:
    return any(isinstance(n, ast.Return) for stmt in body for n in ast.walk(stmt))


def _read_unit(node: ast.AST, name: str, class_name: str | None, entry_point: bool, facts: _ModuleFacts) -> _Unit:
    """Collect the calls and spawns of one function body, nested defs included."""
    unit = _Unit(name=name, class_name=class_name, entry_point=entry_point, calls=set(), self_calls=set(), spawns=[])
    simple = name.rsplit(QUALIFIED_SEPARATOR, 1)[-1]
    spawn_callees = THREAD_SPAWN_CALLEES | facts.thread_classes
    # Iterative walk carrying the depth, so a spawn knows what encloses it: a
    # node's descendants are the ones visited at a deeper depth before the walk
    # returns to its own depth or shallower. Every loop entered takes a serial,
    # so a guard knows whether a loop opened after it was read (a loop opened
    # before the guard encloses it and leaves the bound alone).
    loops_entered = 0
    open_loops: list[tuple[int, int]] = []  # (depth, serial)
    guards: list[tuple[int, int]] = []  # (depth, loops entered when the guard was read)
    early_exit_mark: int | None = None
    pending: list[tuple[ast.AST, int]] = [(node, 0)]
    while pending:
        current, depth = pending.pop()
        while guards and guards[-1][0] >= depth:
            guards.pop()
        while open_loops and open_loops[-1][0] >= depth:
            open_loops.pop()
        if isinstance(current, LOOP_NODES):
            loops_entered += 1
            open_loops.append((depth, loops_entered))
        if isinstance(current, ast.If) and _reads_liveness(current.test):
            guards.append((depth, loops_entered))
            if _returns(current.body):
                early_exit_mark = loops_entered
            # The guard covers the `if`'s test and body. Its `orelse` runs when
            # the test did not hold, so it is queued at the `if`'s own depth,
            # where the walk closes the guard before reading it.
            pending.extend((stmt, depth) for stmt in reversed(current.orelse))
            pending.extend((child, depth + 1) for child in reversed([current.test, *current.body]))
            continue
        if isinstance(current, ast.Call):
            callee = _callee_name(current)
            if callee in spawn_callees:
                looped_since = {serial for _, serial in open_loops}
                if simple == STARTUP_FUNCTION:
                    bound: str | None = BOUND_STARTUP
                elif guards and not any(serial > guards[-1][1] for serial in looped_since):
                    bound = BOUND_SINGLETON
                elif early_exit_mark is not None and not any(serial > early_exit_mark for serial in looped_since):
                    # The early return bounds the one spawn that follows it,
                    # not every later spawn in the function.
                    bound = BOUND_SINGLETON
                    early_exit_mark = None
                else:
                    bound = None
                unit.spawns.append((current.lineno, bound))
            elif callee is not None:
                receiver = current.func.value if isinstance(current.func, ast.Attribute) else None
                if receiver is None:
                    unit.calls.add(callee)
                elif isinstance(receiver, ast.Name) and receiver.id in SELF_RECEIVERS:
                    unit.self_calls.add(callee)
            if callee is not None:
                # The function a spawn runs, or one handed to add_task/submit,
                # is the request's work too: an edge as if it were called.
                for value in _handed_on(current, callee):
                    if isinstance(value, ast.Name):
                        unit.calls.add(value.id)
                    elif (
                        isinstance(value, ast.Attribute)
                        and isinstance(value.value, ast.Name)
                        and value.value.id in SELF_RECEIVERS
                    ):
                        unit.self_calls.add(value.attr)
        # Reversed so the walk visits children in source order with a stack.
        for child in reversed(list(ast.iter_child_nodes(current))):
            pending.append((child, depth + 1))
    return unit


def _module_facts(module: ast.Module) -> _ModuleFacts:
    thread_classes: set[str] = set()
    for node in module.body:
        if isinstance(node, ast.ClassDef) and any(b in THREAD_BASES for b in _base_names(node)):
            thread_classes.add(node.name)
    return _ModuleFacts(frozenset(thread_classes))


def _units_of(module: ast.Module) -> list[_Unit]:
    facts = _module_facts(module)
    units: list[_Unit] = []
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entry = any(_is_route_decorator(d) for d in node.decorator_list)
            units.append(_read_unit(node, node.name, None, entry, facts))
        elif isinstance(node, ast.ClassDef):
            handler_class = _is_request_handler_class(node)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    entry = handler_class or any(_is_route_decorator(d) for d in item.decorator_list)
                    units.append(_read_unit(item, f"{node.name}{QUALIFIED_SEPARATOR}{item.name}", node.name, entry, facts))
    return units


def _reachability(units: list[_Unit]) -> dict[str, set[str]]:
    """For every unit name, the entry points whose direct-call closure contains it."""
    by_simple: dict[str, list[_Unit]] = {}
    by_class: dict[str, dict[str, _Unit]] = {}
    for unit in units:
        simple = unit.name.rsplit(QUALIFIED_SEPARATOR, 1)[-1]
        if unit.class_name is None:
            by_simple.setdefault(simple, []).append(unit)
        else:
            by_class.setdefault(unit.class_name, {})[simple] = unit
    edges: dict[str, set[str]] = {}
    for unit in units:
        targets: set[str] = set()
        for callee in unit.calls:
            targets.update(u.name for u in by_simple.get(callee, ()))
            constructor = by_class.get(callee, {}).get(CONSTRUCTOR)
            if constructor is not None:
                targets.add(constructor.name)
        if unit.class_name is not None:
            own = by_class.get(unit.class_name, {})
            targets.update(own[c].name for c in unit.self_calls if c in own)
        edges[unit.name] = targets
    reached: dict[str, set[str]] = {unit.name: set() for unit in units}
    for entry in (u for u in units if u.entry_point):
        queue: deque[str] = deque([entry.name])
        seen = {entry.name}
        while queue:
            current = queue.popleft()
            reached[current].add(entry.name)
            for nxt in edges.get(current, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
    return reached


def scan_python(path: Path, root: Path) -> list[Spawn]:
    """Every thread start in one Python module, with the handlers that reach it."""
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    units = _units_of(module)
    reached = _reachability(units)
    shown = path.relative_to(root).as_posix()
    spawns: list[Spawn] = []
    for unit in units:
        handlers = tuple(sorted(reached[unit.name]))
        for line, bound in unit.spawns:
            spawns.append(Spawn(shown, line, unit.name, KIND_THREAD, handlers, bound))
    return spawns


@dataclass(frozen=True)
class _GoFunction:
    name: str
    file: str
    # The file's lines with comments and strings stripped (`_go_strip`).
    lines: list[str]
    start: int  # 1-indexed, inclusive
    end: int
    # The column of the `{` that opens the body on the declaration line (the
    # line's length when it has none), so the walk starts after the signature.
    body_column: int


@dataclass(frozen=True)
class _GoPosition:
    """What the brace walk of a function body knows at one point of it."""

    # The body's code outside every function literal, up to the point.
    outside_literals: str
    # Whether the point sits inside a function literal opened before it.
    in_literal: bool
    # The code of the innermost block holding the point, from the `{` that
    # opens it to the point, nested blocks left out.
    block: str


def _go_strip(lines: list[str]) -> list[str]:
    """The file's lines with comments and string and rune literals removed.

    One output line per input line, so line numbers hold; the inner lines of a
    raw string or a block comment that spans lines come out empty.
    """
    stripped: list[str] = []
    in_block_comment = False
    in_raw_string = False
    for text in lines:
        kept: list[str] = []
        index = 0
        while index < len(text):
            if in_block_comment:
                close = text.find(GO_BLOCK_COMMENT_CLOSE, index)
                if close < 0:
                    break
                in_block_comment = False
                index = close + len(GO_BLOCK_COMMENT_CLOSE)
                continue
            if in_raw_string:
                close = text.find(GO_RAW_QUOTE, index)
                if close < 0:
                    break
                in_raw_string = False
                index = close + 1
                continue
            if text.startswith(GO_LINE_COMMENT, index):
                break
            if text.startswith(GO_BLOCK_COMMENT_OPEN, index):
                in_block_comment = True
                index += len(GO_BLOCK_COMMENT_OPEN)
                continue
            char = text[index]
            if char == GO_RAW_QUOTE:
                in_raw_string = True
                index += 1
                continue
            if char in GO_QUOTES:
                index += 1
                while index < len(text) and text[index] != char:
                    index += 2 if text[index] == GO_ESCAPE else 1
                index += 1
                continue
            kept.append(char)
            index += 1
        stripped.append("".join(kept))
    return stripped


def _go_body_column(text: str, start: int) -> int:
    """The column of the `{` that opens a function body on its declaration line.

    The first `{` after `start` outside parentheses and brackets that no
    `struct` or `interface` precedes, so a parameter or result type with its
    own braces is skipped; the line's length when there is none.
    """
    nesting = 0
    for index in range(start, len(text)):
        char = text[index]
        if char in GO_NEST_OPEN:
            nesting += 1
        elif char in GO_NEST_CLOSE:
            nesting -= 1
        elif char == GO_BRACE_OPEN and nesting == 0 and GO_TYPE_BLOCK_RE.search(text[:index]) is None:
            return index
    return len(text)


def _go_functions(shown: str, lines: list[str]) -> list[_GoFunction]:
    """Every top-level function of one stripped file, with its line span and body column."""
    functions: list[_GoFunction] = []
    current: tuple[str, int, int] | None = None
    for number, text in enumerate(lines, 1):
        match = GO_FUNC_RE.match(text)
        if match is not None:
            if current is not None:
                functions.append(_GoFunction(current[0], shown, lines, current[1], number - 1, current[2]))
            current = (match.group(1), number, _go_body_column(text, match.end()))
            # A one-line function closes on its declaration line.
            if GO_BRACE_OPEN in text and text.count(GO_BRACE_OPEN) == text.count(GO_BRACE_CLOSE):
                functions.append(_GoFunction(current[0], shown, lines, number, number, current[2]))
                current = None
        elif current is not None and text.rstrip() == GO_FUNC_END:
            functions.append(_GoFunction(current[0], shown, lines, current[1], number, current[2]))
            current = None
    if current is not None:
        functions.append(_GoFunction(current[0], shown, lines, current[1], len(lines), current[2]))
    return functions


def _go_walk(function: _GoFunction, line: int | None = None, column: int = 0) -> _GoPosition:
    """Walk the braces of a function body from the `{` that opens it to a point.

    The point is `column` characters into `line`, or the end of the function
    when `line` is None. The signature is skipped (the walk starts after the
    body's `{` on the declaration line), so a function type in it opens
    nothing. A `func(` opens a literal when the `{` after its parameter list
    comes before a `;` or the end of that list's line; the literal is open
    until that brace's pair closes.
    """
    depth = 0
    # The parenthesis depth of a `func(` parameter list being read, or None.
    params_open: int | None = None
    literal_pending = False
    open_depths: list[int] = []
    outside: list[str] = []
    blocks: list[list[str]] = [[]]
    stop = function.end if line is None else line
    for number in range(function.start, stop + 1):
        code = function.lines[number - 1]
        if line is not None and number == stop:
            code = code[:column]
        if number == function.start:
            code = code[function.body_column + 1 :]
        index = 0
        while index < len(code):
            char = code[index]
            step = 1
            if params_open is None and code.startswith(GO_FUNC_LITERAL, index):
                params_open = 1
                literal_pending = False
                step = len(GO_FUNC_LITERAL)
            elif params_open is not None:
                if char == GO_PAREN_OPEN:
                    params_open += 1
                elif char == GO_PAREN_CLOSE:
                    params_open -= 1
                    if params_open == 0:
                        params_open = None
                        literal_pending = True
            elif char == GO_STATEMENT_END:
                literal_pending = False
            elif char == GO_BRACE_OPEN:
                if literal_pending:
                    open_depths.append(depth)
                    literal_pending = False
                depth += 1
                blocks.append([])
            elif char == GO_BRACE_CLOSE:
                depth -= 1
                if open_depths and open_depths[-1] == depth:
                    open_depths.pop()
                if len(blocks) > 1:
                    blocks.pop()
            chunk = code[index : index + step]
            if not open_depths:
                outside.append(chunk)
            blocks[-1].append(chunk)
            index += step
        if params_open is None:
            literal_pending = False
        outside.append("\n")
        blocks[-1].append("\n")
    return _GoPosition("".join(outside), bool(open_depths), "".join(blocks[-1]))


def _go_startup_names(functions: list[_GoFunction]) -> set[str]:
    """`main` and, transitively by name, every function a startup function calls outside its literals."""
    by_name: dict[str, list[_GoFunction]] = {}
    for function in functions:
        by_name.setdefault(function.name, []).append(function)
    startup: set[str] = set()
    queue: deque[str] = deque([STARTUP_FUNCTION])
    while queue:
        name = queue.popleft()
        if name in startup or name not in by_name:
            continue
        startup.add(name)
        for function in by_name[name]:
            queue.extend(GO_CALL_RE.findall(_go_walk(function).outside_literals))
    return startup


def scan_go(paths: Iterable[Path], root: Path) -> list[Spawn]:
    """Every goroutine start across the Go files, with the bound it sits under if any."""
    files: list[tuple[str, list[str]]] = []
    functions: list[_GoFunction] = []
    for path in paths:
        shown = path.relative_to(root).as_posix()
        lines = _go_strip(path.read_text(encoding="utf-8").splitlines())
        files.append((shown, lines))
        functions.extend(_go_functions(shown, lines))
    startup = _go_startup_names(functions)
    spawns: list[Spawn] = []
    for shown, lines in files:
        own = [f for f in functions if f.file == shown]
        for number, text in enumerate(lines, 1):
            # Every statement on the line, so `go a(); go b()` counts two.
            for match in GO_SPAWN_RE.finditer(text):
                enclosing = next((f for f in own if f.start <= number <= f.end), None)
                if enclosing is None:
                    spawns.append(Spawn(shown, number, GO_NO_FUNCTION, KIND_GOROUTINE, (GO_NO_GRAPH,), None))
                    continue
                position = _go_walk(enclosing, number, match.start(1))
                if enclosing.name == STARTUP_FUNCTION and not position.in_literal:
                    bound: str | None = BOUND_STARTUP
                elif enclosing.name in startup and not position.in_literal:
                    bound = BOUND_GO_STARTUP
                elif GO_SEMAPHORE_RE.search(position.block):
                    bound = BOUND_GO_SEMAPHORE
                else:
                    bound = None
                spawns.append(Spawn(shown, number, enclosing.name, KIND_GOROUTINE, (GO_NO_GRAPH,), bound))
    return spawns


def go_files(base: Path) -> list[Path]:
    """The non-test Go files under one directory, testdata trees excepted."""
    if not base.is_dir():
        return []
    return [
        path
        for path in sorted(base.rglob(f"*{GO_SUFFIX}"))
        if not path.name.endswith(GO_TEST_SUFFIX) and GO_TESTDATA_DIR not in path.relative_to(base).parts
    ]


def scan(root: Path, python_targets: Iterable[str], go_dirs: Iterable[str]) -> tuple[list[Spawn], list[str], list[str]]:
    """Every spawn across the targets, the files read (repository-relative), and the targets that were not there."""
    spawns: list[Spawn] = []
    files: list[str] = []
    problems: list[str] = []
    for rel in python_targets:
        path = root / rel
        if not path.is_file():
            problems.append(TARGET_MISSING_ERROR.format(target=rel, script=SCRIPT_NAME))
            continue
        files.append(path.relative_to(root).as_posix())
        spawns.extend(scan_python(path, root))
    go_paths: list[Path] = []
    for rel in go_dirs:
        found = go_files(root / rel)
        if not found:
            problems.append(GO_DIR_EMPTY_ERROR.format(target=rel, script=SCRIPT_NAME))
            continue
        go_paths.extend(found)
    files.extend(path.relative_to(root).as_posix() for path in go_paths)
    spawns.extend(scan_go(go_paths, root))
    return spawns, files, problems


def display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def load_allowlist(path: Path, shown: str) -> tuple[list[AllowlistEntry], list[str]]:
    """Every entry of the allowlist, and the lines that are not one."""
    entries: list[AllowlistEntry] = []
    errors: list[str] = []
    first_seen: dict[tuple[str, str], int] = {}
    if not path.is_file():
        return entries, errors
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(COMMENT_PREFIX):
            continue
        fields = line.split()
        count = int(fields[2]) if len(fields) == ALLOWLIST_FIELDS and fields[2].isdecimal() else 0
        if count <= 0:
            errors.append(ALLOWLIST_MALFORMED_ERROR.format(allowlist=shown, line=number, text=line))
            continue
        key = (fields[0], fields[1])
        if key in first_seen:
            errors.append(
                ALLOWLIST_DUPLICATE_ERROR.format(
                    allowlist=shown, line=number, file=key[0], function=key[1], first=first_seen[key]
                )
            )
            continue
        first_seen[key] = number
        entries.append(AllowlistEntry(key[0], key[1], count, number))
    return entries, errors


def reconcile(
    spawns: list[Spawn], files: list[str], entries: list[AllowlistEntry], shown: str
) -> tuple[list[Spawn], list[str]]:
    """Flagged spawns no entry covers, and the entries the source no longer matches exactly."""
    flagged: dict[tuple[str, str], list[Spawn]] = {}
    for spawn in spawns:
        if spawn.flagged:
            flagged.setdefault((spawn.file, spawn.function), []).append(spawn)
    problems: list[str] = []
    covered: set[tuple[str, str]] = set()
    scanned = set(files)
    for entry in entries:
        key = (entry.file, entry.function)
        fields = dict(allowlist=shown, line=entry.line, file=entry.file, function=entry.function, allowed=entry.count)
        if entry.file not in scanned:
            problems.append(ALLOWLIST_UNSCANNED_ERROR.format(**fields))
            continue
        found = len(flagged.get(key, ()))
        covered.add(key)
        if found == 0:
            problems.append(ALLOWLIST_STALE_ERROR.format(**fields))
        elif found > entry.count:
            problems.append(ALLOWLIST_RISE_ERROR.format(found=found, **fields))
        elif found < entry.count:
            problems.append(ALLOWLIST_DROP_ERROR.format(found=found, **fields))
    findings = [s for key, group in sorted(flagged.items()) if key not in covered for s in group]
    return findings, problems


def check(
    root: Path = REPO_ROOT,
    python_targets: Iterable[str] = PYTHON_TARGETS,
    go_dirs: Iterable[str] = GO_TARGET_DIRS,
    allowlist: Path = ALLOWLIST,
    list_spawns: bool = False,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    shown = display_path(allowlist, root)
    spawns, files, missing = scan(root, python_targets, go_dirs)
    entries, malformed = load_allowlist(allowlist, shown)
    findings, problems = reconcile(spawns, files, entries, shown)
    problems = missing + malformed + problems
    if list_spawns:
        for spawn in spawns:
            print(
                LIST_LINE.format(
                    file=spawn.file,
                    line=spawn.line,
                    function=spawn.function,
                    kind=spawn.kind,
                    handlers=",".join(spawn.reachable_from) or "-",
                    bounded=spawn.bounded_by or "-",
                ),
                file=out,
            )
    for spawn in findings:
        print(
            FINDING_LINE.format(
                file=spawn.file,
                line=spawn.line,
                function=spawn.function,
                kind=spawn.kind,
                handlers=", ".join(spawn.reachable_from),
                allowlist=shown,
            ),
            file=err,
        )
    for problem in problems:
        print(problem, file=err)
    if findings or problems:
        print(SUMMARY_FAIL.format(findings=len(findings), problems=len(problems)), file=err)
        return EXIT_FAIL
    print(SUMMARY_OK.format(spawns=len(spawns), files=len(files), entries=len(entries)), file=out)
    return EXIT_OK


def main(argv: list[str] | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root the targets are relative to")
    parser.add_argument(
        "--python-target", action="append", default=None, help="a Python module to read (repeatable; default: the targets)"
    )
    parser.add_argument(
        "--go-dir", action="append", default=None, help="a directory of Go files to read (repeatable; default: the callout)"
    )
    parser.add_argument("--allowlist", type=Path, default=None, help="the allowlist file (default: scripts/bounded_concurrency_allowlist.txt)")
    parser.add_argument("--list", action="store_true", help="print every spawn with its classification")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    python_targets = tuple(args.python_target) if args.python_target is not None else PYTHON_TARGETS
    go_dirs = tuple(args.go_dir) if args.go_dir is not None else GO_TARGET_DIRS
    allowlist = args.allowlist if args.allowlist is not None else (ALLOWLIST if root == REPO_ROOT else root / ALLOWLIST.relative_to(REPO_ROOT))
    return check(root, python_targets, go_dirs, allowlist, list_spawns=args.list, out=out, err=err)


if __name__ == "__main__":
    sys.exit(main())
