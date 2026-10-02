#!/usr/bin/env python3
"""Build gate for the quiet rate-limit exit patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_quiet_rate_limit_exit.py``. The applier proves its anchor matched once;
this proves the patched exit block behaves: executed against the real
``cli.py`` text with a stand-in ``result`` and no ``HERMES_KANBAN_TASK`` in the
environment, a ``rate_limit`` failure exits 75, a ``billing`` failure exits 75,
any other failure exits 1, and a turn that did not fail exits 0.

The block is located with ``ast``: the ``_exit_code = 0`` assignment in the
one-shot function and the ``sys.exit(_exit_code)`` call that ends it, so an
upstream reformat is handled or reported, never a traceback out of a build.

Usage::

    cd /opt/hermes && python3 verify_quiet_rate_limit_exit.py
"""

from __future__ import annotations

import ast
import os
import sys
import textwrap
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
FAILURES: list[str] = []
MARKER = "kube-agents patch: quiet_rate_limit_exit"


def fail(msg: str) -> None:
    FAILURES.append(msg)


def _assigns(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Assign) and len(node.targets) == 1 and \
        isinstance(node.targets[0], ast.Name) and node.targets[0].id == name


def _is_sys_exit_of(node: ast.AST, name: str) -> bool:
    if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
        return False
    f = node.value.func
    return isinstance(f, ast.Attribute) and f.attr == "exit" and isinstance(f.value, ast.Name) \
        and f.value.id == "sys" and len(node.value.args) == 1 \
        and isinstance(node.value.args[0], ast.Name) and node.value.args[0].id == name


def exit_block(source: str) -> str:
    """The statements from ``_exit_code = 0`` to ``sys.exit(_exit_code)``, dedented."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        fail(f"cli.py does not parse: {exc}")
        return ""
    starts = [n for n in ast.walk(tree) if _assigns(n, "_exit_code")
              and isinstance(n.value, ast.Constant) and n.value.value == 0]
    if len(starts) != 1:
        fail(f"expected one `_exit_code = 0` in cli.py, found {len(starts)}; the exit block moved")
        return ""
    start = starts[0]
    end = next((n for n in ast.walk(tree) if _is_sys_exit_of(n, "_exit_code")
                and n.lineno > start.lineno and n.col_offset == start.col_offset), None)
    if end is None:
        fail("no `sys.exit(_exit_code)` after the assignment; the exit block moved")
        return ""
    lines = source.splitlines(keepends=True)
    block = textwrap.dedent("".join(lines[start.lineno - 1 : end.end_lineno]))
    if MARKER not in block:
        fail("the patch marker is not inside the exit block; the kanban-only condition is still there or moved")
    return block


class _Exit(Exception):
    def __init__(self, code):
        self.code = code


def run_block(block: str, result) -> int | None:
    """Execute the block with a stand-in ``result``; return the exit code it chose."""
    try:
        code = compile(block, "<exit-block>", "exec")
    except SyntaxError as exc:
        fail(f"the exit block does not compile on its own: {exc}")
        return None

    class _Sys:
        @staticmethod
        def exit(c):
            raise _Exit(c)

    env = {k: v for k, v in os.environ.items() if k != "HERMES_KANBAN_TASK"}

    class _OS:
        environ = env

    class _KanbanDB:
        KANBAN_RATE_LIMIT_EXIT_CODE = 75

    import types
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.kanban_db = _KanbanDB  # type: ignore[attr-defined]
    ns = {"result": result, "os": _OS, "sys": _Sys, "__builtins__": __builtins__}
    real_modules = sys.modules.get("hermes_cli"), sys.modules.get("hermes_cli.kanban_db")
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.kanban_db"] = _KanbanDB  # type: ignore[assignment]
    try:
        exec(code, ns)
    except _Exit as e:
        return e.code
    except Exception as exc:  # noqa: BLE001 -- any failure is the build's to see
        fail(f"the exit block raised when run: {exc!r}")
        return None
    finally:
        for name, mod in zip(("hermes_cli", "hermes_cli.kanban_db"), real_modules):
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
    fail("the exit block returned without calling sys.exit")
    return None


def main() -> int:
    cli = HERMES / "cli.py"
    source = cli.read_text()
    block = exit_block(source)
    if block:
        try:
            compile(source, str(cli), "exec")
        except SyntaxError as exc:
            fail(f"cli.py does not compile after the patch: {exc}")
        cases = [
            ({"failed": True, "failure_reason": "rate_limit"}, 75, "a rate-limit failure"),
            ({"failed": True, "failure_reason": "billing"}, 75, "a billing failure"),
            ({"failed": True, "failure_reason": "tool_error"}, 1, "any other failure"),
            ({"failed": False, "final_response": "ok"}, 0, "a turn that did not fail"),
        ]
        for result, want, what in cases:
            got = run_block(block, result)
            if got is not None and got != want:
                fail(f"{what} exits {got}, want {want}")
    if FAILURES:
        for f in FAILURES:
            print(f"verify_quiet_rate_limit_exit: {f}", file=sys.stderr)
        return 1
    print("verify_quiet_rate_limit_exit: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
