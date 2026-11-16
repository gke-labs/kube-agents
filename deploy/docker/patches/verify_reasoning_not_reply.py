#!/usr/bin/env python3
"""Build gate for the reasoning-not-reply patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_reasoning_not_reply.py``. The applier proves its three anchors matched
once each; this proves what the patch is for, read from the patched tree with
``ast``:

- every patched file parses and carries the marker (the two gateway files,
  the ``/reasoning show`` refusal and the prepend, are proved by the marker
  and the applier's anchors alone);
- ``_terminal_empty`` returns nothing built from the reasoning;
- the only direct calls of ``_extract_reasoning`` (or the ``extract_reasoning``
  helper it forwards to) left under ``agent/`` and in ``run_agent.py`` are the
  two known not to make a reply of it: the one that stores the reasoning on the
  assistant message, and ``_terminal_empty``, which logs it. A Hermes bump that
  adds a direct call there fails here, so the new site gets read before the
  image ships: if it can turn reasoning into reply text, it needs the same
  treatment. Not seen: a call through an alias, a callback or ``getattr``, and
  code that reads ``reasoning_content`` off a message directly. The gate
  narrows the search, it does not replace reading a bump's diff.

Usage::

    cd /opt/hermes && python3 verify_reasoning_not_reply.py
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
FAILURES: list[str] = []
MARKER = "kube-agents patch: reasoning_not_reply"
PATCHED = (
    "agent/turn_final_response.py",
    "agent/turn_empty_response.py",
    "agent/turn_truncation.py",
    "gateway/slash_commands_model.py",
    "gateway/run_turn.py",
)
TERMINAL_FILE = "agent/turn_empty_response.py"
TERMINAL_FUNCTION = "_terminal_empty"
REASONING_NAMES = frozenset({"reasoning_text", "reasoning_preview"})
READER = "_extract_reasoning"
# The helper the method forwards to (agent/agent_runtime_helpers.py); a call
# to it by name or attribute reads the same channel.
READER_HELPER = "extract_reasoning"
READER_NAMES = frozenset({READER, READER_HELPER})
# (file, enclosing function) -> how many direct calls it may hold. A count, not a set, so a
# second call added inside an allowed function fails too.
ALLOWED_READERS = {
    ("agent/chat_completion_helpers.py", "_assistant_reasoning_text"): 1,
    (TERMINAL_FILE, TERMINAL_FUNCTION): 1,
}
SCANNED_ROOT_FILES = ("run_agent.py",)
SCANNED_DIR = "agent"


def fail(msg: str) -> None:
    FAILURES.append(msg)


def _parse(relative: str) -> ast.Module | None:
    path = HERMES / relative
    try:
        return ast.parse(path.read_text(), filename=str(path))
    except (OSError, SyntaxError) as exc:
        fail(f"{relative} does not read or parse: {exc}")
        return None


def _reader_calls(tree: ast.Module) -> list[tuple[str, int]]:
    """(enclosing function, line) of every call that reads the reasoning channel:
    ``<x>._extract_reasoning(...)``, or ``extract_reasoning(...)`` by name or attribute."""
    found: list[tuple[str, int]] = []

    def walk(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else enclosing
            if isinstance(child, ast.Call):
                func = child.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
                if called in READER_NAMES:
                    found.append((enclosing, child.lineno))
            walk(child, name)

    walk(tree, "<module>")
    return found


def check_markers() -> None:
    for relative in PATCHED:
        path = HERMES / relative
        if MARKER not in path.read_text():
            fail(f"{relative} carries no {MARKER!r}; the patch did not run")
        _parse(relative)


def check_terminal_returns() -> None:
    tree = _parse(TERMINAL_FILE)
    if tree is None:
        return
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == TERMINAL_FUNCTION]
    if len(functions) != 1:
        fail(f"expected one {TERMINAL_FUNCTION} in {TERMINAL_FILE}, found {len(functions)}")
        return
    for node in ast.walk(functions[0]):
        if isinstance(node, ast.Return) and node.value is not None:
            used = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)} & REASONING_NAMES
            if used:
                fail(f"{TERMINAL_FUNCTION} line {node.lineno} returns text built from {sorted(used)}")


def check_readers() -> None:
    files = list(SCANNED_ROOT_FILES) + sorted(
        str(p.relative_to(HERMES)) for p in (HERMES / SCANNED_DIR).rglob("*.py")
    )
    seen: dict[tuple[str, str], list[int]] = {}
    for relative in files:
        tree = _parse(relative)
        if tree is None:
            continue
        for enclosing, line in _reader_calls(tree):
            seen.setdefault((relative, enclosing), []).append(line)
            if (relative, enclosing) not in ALLOWED_READERS:
                fail(
                    f"{relative}:{line} ({enclosing}) calls {READER}; read it, and if it can make the "
                    f"reasoning a reply, extend apply_reasoning_not_reply.py, else add it to ALLOWED_READERS"
                )
    for (relative, enclosing), expected in sorted(ALLOWED_READERS.items()):
        lines = seen.get((relative, enclosing), [])
        if not lines:
            fail(f"no {READER} call left in {relative} ({enclosing}); upstream moved it, re-derive ALLOWED_READERS")
        elif len(lines) != expected:
            fail(
                f"{relative} ({enclosing}) calls {READER} {len(lines)} times (lines {lines}), {expected} expected; "
                f"read the new call, and if it can make the reasoning a reply, extend apply_reasoning_not_reply.py"
            )


def main() -> int:
    check_markers()
    check_terminal_returns()
    check_readers()
    if FAILURES:
        for f in FAILURES:
            print(f"verify_reasoning_not_reply: {f}", file=sys.stderr)
        return 1
    print("verify_reasoning_not_reply: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
