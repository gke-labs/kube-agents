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
instantiation (an edge to its `__init__`), and `self.name(...)` or
`cls.name(...)` to the same class's method. A call through any other receiver (a
module-level singleton, a stored callback, a thread's `target=`) is not
followed; that is the check's blind spot, named here so a reviewer knows it. A
spawn (`threading.Thread(...)`, `_thread.start_new_thread(...)`) in a function a
handler reaches is a finding unless its shape is bounded:

* a fixed-size pool: the spawn sits in a loop or comprehension over `range(...)`,
  so its count is fixed when the pool is built, not per request;
* a singleton guard: the function checks `.is_alive()` before spawning, so at most
  one such thread runs at a time;
* startup: the function is `main`.

A `ThreadPoolExecutor` is not a spawn, so a bounded executor needs no shape.

Go (`GO_TARGET_DIRS`), by text, since the callout has no request-handler marker
to anchor a graph on: every `go func(...)` or `go name(...)` statement in a
non-test file is a finding unless it sits in `main`, in a function `main` calls
directly (the process's startup path), or in a function that bounds it with an
`errgroup` `SetLimit(...)`, a `semaphore` package call, or a send on a channel
whose name carries `sem`.

``scripts/bounded_concurrency_allowlist.txt``: one line per function that
carried a finding when the check arrived, `<file> <function> <count>`, with the
issue that owns the fix in a comment above it. The list only shrinks: a count
above the entry fails (a rise), a count below fails until the entry is lowered
or removed (a drop), an entry whose function has no finding left fails (stale),
and a line that is not an entry fails. A function with no entry is held at zero.

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
from typing import Iterable, Iterator

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
# The callee names that start a thread. `Thread` covers `threading.Thread(...)`
# and a bare `Thread(...)` import; `start_new_thread` is the `_thread` primitive.
THREAD_SPAWN_CALLEES = frozenset({"Thread", "start_new_thread"})
# The bounded shapes, and the words the report uses for them.
SINGLETON_GUARD_ATTR = "is_alive"
FIXED_SIZE_ITERATOR = "range"
STARTUP_FUNCTION = "main"
SELF_RECEIVERS = frozenset({"self", "cls"})
BOUND_FIXED_SIZE = "fixed-size pool (a loop over range)"
BOUND_SINGLETON = "singleton guard (is_alive)"
BOUND_STARTUP = "startup (main)"
BOUND_CALLED_FROM_MAIN = "startup (called from main)"
BOUND_GO_LIMIT = "SetLimit or a semaphore"
CONSTRUCTOR = "__init__"
QUALIFIED_SEPARATOR = "."

# Go, by text. A spawn is a line that starts a goroutine; a function starts at
# column 0 with `func`, optionally with a receiver, and ends at the first `}` at
# column 0 (gofmt's shape, which `go vet` in CI holds).
GO_SPAWN_RE = re.compile(r"^\s*go\s+(?:func\b|[A-Za-z_][\w.]*\()")
GO_FUNC_RE = re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)")
GO_FUNC_END = "}"
GO_BOUNDED_RE = re.compile(r"\.SetLimit\(|\bsemaphore\.|\b\w*[sS]em\w*\s*<-")
GO_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\(")
GO_NO_GRAPH = "(every function; Go has no call graph here)"
KIND_THREAD = "thread"
KIND_GOROUTINE = "goroutine"

# How the report reads.
FINDING_LINE = "{file}:{line}: {function} starts a {kind} per request, reachable from {handlers}; bound it or pin it in {allowlist}"
LIST_LINE = "{file}:{line}: {function} {kind} reachable_from={handlers} bounded_by={bounded}"
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
SUMMARY_FAIL = "check_bounded_concurrency: {findings} finding(s) not pinned and {problems} allowlist problem(s)"
SUMMARY_OK = "check_bounded_concurrency: {spawns} spawn(s) read across {files} file(s), none unbounded beyond the {entries} allowlist entr(y/ies)"
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


def _callee_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_route_decorator(decorator: ast.expr) -> bool:
    return (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr in ROUTE_DECORATOR_ATTRS
    )


def _is_request_handler_class(node: ast.ClassDef) -> bool:
    for base in node.bases:
        name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
        if name.endswith(REQUEST_HANDLER_BASE_SUFFIX):
            return True
    return False


def _is_fixed_size_loop(node: ast.AST) -> bool:
    """A `for` or comprehension whose iterator is `range(...)`."""
    iterators: list[ast.expr] = []
    if isinstance(node, (ast.For, ast.AsyncFor)):
        iterators.append(node.iter)
    elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
        iterators.extend(gen.iter for gen in node.generators)
    return any(isinstance(it, ast.Call) and _callee_name(it) == FIXED_SIZE_ITERATOR for it in iterators)


def _read_unit(node: ast.AST, name: str, class_name: str | None, entry_point: bool) -> _Unit:
    """Collect the calls and spawns of one function body, nested defs included."""
    unit = _Unit(name=name, class_name=class_name, entry_point=entry_point, calls=set(), self_calls=set(), spawns=[])
    guarded = False
    fixed_depths: list[int] = []
    spawn_calls: list[tuple[ast.Call, bool]] = []
    # Iterative walk carrying the depth, so a spawn knows whether a range loop
    # encloses it: a loop's descendants are the nodes visited at a deeper depth
    # before the walk returns to its own.
    pending: list[tuple[ast.AST, int]] = [(node, 0)]
    while pending:
        current, depth = pending.pop()
        while fixed_depths and fixed_depths[-1] >= depth:
            fixed_depths.pop()
        if _is_fixed_size_loop(current):
            fixed_depths.append(depth)
        if isinstance(current, ast.Call):
            callee = _callee_name(current)
            if callee in THREAD_SPAWN_CALLEES:
                spawn_calls.append((current, bool(fixed_depths)))
            elif callee is not None:
                receiver = current.func.value if isinstance(current.func, ast.Attribute) else None
                if receiver is None:
                    unit.calls.add(callee)
                elif isinstance(receiver, ast.Name) and receiver.id in SELF_RECEIVERS:
                    unit.self_calls.add(callee)
                if callee == SINGLETON_GUARD_ATTR:
                    guarded = True
        children = list(ast.iter_child_nodes(current))
        # Reversed so the walk visits children in source order with a stack.
        for child in reversed(children):
            pending.append((child, depth + 1))
    simple = name.rsplit(QUALIFIED_SEPARATOR, 1)[-1]
    for call, in_fixed_loop in spawn_calls:
        if simple == STARTUP_FUNCTION:
            bound: str | None = BOUND_STARTUP
        elif in_fixed_loop:
            bound = BOUND_FIXED_SIZE
        elif guarded:
            bound = BOUND_SINGLETON
        else:
            bound = None
        unit.spawns.append((call.lineno, bound))
    return unit


def _units_of(module: ast.Module) -> list[_Unit]:
    units: list[_Unit] = []
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entry = any(_is_route_decorator(d) for d in node.decorator_list)
            units.append(_read_unit(node, node.name, None, entry))
        elif isinstance(node, ast.ClassDef):
            handler_class = _is_request_handler_class(node)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    entry = handler_class or any(_is_route_decorator(d) for d in item.decorator_list)
                    units.append(_read_unit(item, f"{node.name}{QUALIFIED_SEPARATOR}{item.name}", node.name, entry))
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


def _go_functions(lines: list[str]) -> list[tuple[str, int, int]]:
    """(name, first line, last line) of every top-level function, 1-indexed and inclusive."""
    functions: list[tuple[str, int, int]] = []
    current: tuple[str, int] | None = None
    for number, text in enumerate(lines, 1):
        match = GO_FUNC_RE.match(text)
        if match is not None:
            if current is not None:
                functions.append((current[0], current[1], number - 1))
            current = (match.group(1), number)
        elif current is not None and text.rstrip("\r\n") == GO_FUNC_END:
            functions.append((current[0], current[1], number))
            current = None
    if current is not None:
        functions.append((current[0], current[1], len(lines)))
    return functions


def scan_go(path: Path, root: Path) -> list[Spawn]:
    """Every goroutine start in one Go file, with the bound it sits under if any."""
    lines = path.read_text(encoding="utf-8").splitlines()
    functions = _go_functions(lines)
    shown = path.relative_to(root).as_posix()
    called_from_main: set[str] = set()
    for name, start, end in functions:
        if name == STARTUP_FUNCTION:
            body = "\n".join(lines[start - 1 : end])
            called_from_main.update(GO_CALL_RE.findall(body))
    spawns: list[Spawn] = []
    for number, text in enumerate(lines, 1):
        if not GO_SPAWN_RE.match(text):
            continue
        enclosing = next(((n, s) for n, s, e in functions if s <= number <= e), None)
        if enclosing is None:
            spawns.append(Spawn(shown, number, "", KIND_GOROUTINE, (GO_NO_GRAPH,), None))
            continue
        name, start = enclosing
        before = "\n".join(lines[start - 1 : number])
        if name == STARTUP_FUNCTION:
            bound: str | None = BOUND_STARTUP
        elif name in called_from_main:
            bound = BOUND_CALLED_FROM_MAIN
        elif GO_BOUNDED_RE.search(before):
            bound = BOUND_GO_LIMIT
        else:
            bound = None
        spawns.append(Spawn(shown, number, name, KIND_GOROUTINE, (GO_NO_GRAPH,), bound))
    return spawns


def go_files(root: Path, dirs: Iterable[str]) -> Iterator[Path]:
    for rel in dirs:
        base = root / rel
        if not base.is_dir():
            continue
        for path in sorted(base.rglob(f"*{GO_SUFFIX}")):
            if path.name.endswith(GO_TEST_SUFFIX) or GO_TESTDATA_DIR in path.relative_to(base).parts:
                continue
            yield path


def scan(root: Path, python_targets: Iterable[str], go_dirs: Iterable[str]) -> tuple[list[Spawn], list[str]]:
    """Every spawn across the targets, and the files read (repository-relative)."""
    spawns: list[Spawn] = []
    files: list[str] = []
    for rel in python_targets:
        path = root / rel
        if not path.is_file():
            continue
        files.append(path.relative_to(root).as_posix())
        spawns.extend(scan_python(path, root))
    for path in go_files(root, go_dirs):
        files.append(path.relative_to(root).as_posix())
        spawns.extend(scan_go(path, root))
    return spawns, files


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
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    shown = display_path(allowlist, root)
    spawns, files = scan(root, python_targets, go_dirs)
    entries, malformed = load_allowlist(allowlist, shown)
    findings, problems = reconcile(spawns, files, entries, shown)
    problems = malformed + problems
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


def main(argv: list[str] | None = None) -> int:
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
    return check(root, python_targets, go_dirs, allowlist, list_spawns=args.list)


if __name__ == "__main__":
    sys.exit(main())
