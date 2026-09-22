#!/usr/bin/env python3
"""Wire tools/kanban_worker_tools.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. Two edits in one
file — an import block and one gate override inside the registration loop —
with the same guarantee as the other patches here: every edit site must be
found exactly once, the file must still parse, and anything else fails the
build loudly rather than shipping a half-patched image.

**Located by AST, not by literal anchors.** Upstream registers its kanban tools
table-driven since v2026.9.14: a ``_TOOLS`` tuple of
``(name, schema, handler, emoji)`` rows and one ``for`` loop that picks a gate
per row (``_gate = _check_kanban_orchestrator_mode if _name in
_ORCHESTRATOR_TOOLS else _check_kanban_mode``) and calls ``registry.register``
once. There is no longer a ``registry.register(name="kanban_complete", ...)``
statement to locate, so the per-tool ``check_fn`` splices this applier
used to make became a single override of ``_gate`` inside that loop, keyed on
``WORKER_ONLY_TOOLS`` imported from the companion module.

What the per-tool locators *were* asserting is preserved. Each worker-only
tool must still have exactly one row in ``_TOOLS`` naming its schema constant
and its handler (so a tool upstream has rewired underneath us fails the build
instead of being silently re-gated), must not sit in upstream's own
``_ORCHESTRATOR_TOOLS`` set (which would already hide it from workers), and the
``_gate`` assignment must still be the conditional between upstream's two
gates. See ``patchlib.Patch.find_assign``.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_worker_tools.py``. Usage::

    python3 apply_kanban_worker_tools.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import patchlib
from kanban_worker_tools import WORKER_ONLY_TOOLS

RELATIVE = "tools/kanban_tools.py"

# The import lands after this function rather than at the end of the file the
# way the kanban_notifier patch does. `_gate` is evaluated at import time, in a
# loop several hundred lines above the end of the module, so a trailing import
# would raise NameError before it ever ran. `_check_kanban_orchestrator_mode` is
# the last definition above the handlers that this patch has a reason to name —
# it is the gate `check_kanban_worker_mode` mirrors.
IMPORT_AFTER = "_check_kanban_orchestrator_mode"

# `no_cache_check_fn` is the registry's opt-out from its 30s check_fn memo. Both
# upstream gates carry it because their answer depends on the delegated-child
# ContextVar, which the memo key (function, profile) cannot see; this gate reads
# the same ContextVar and gets the same posture. It marks the function and
# returns it, so the registry still binds the companion module's own callable.
IMPORT_BLOCK = (
    "\n\n"
    "# kube-agents patch: see tools/kanban_worker_tools.py\n"
    "from tools.kanban_worker_tools import (\n"
    "    WORKER_ONLY_TOOLS as _WORKER_ONLY_TOOLS,\n"
    "    check_kanban_worker_mode as _check_kanban_worker_mode,\n"
    ")\n"
    "\n"
    "no_cache_check_fn(_check_kanban_worker_mode)\n"
)

#: The two gates upstream's registration loop chooses between, and the one this
#: patch adds. Asserting the loop still chooses between exactly these is what
#: catches an upstream regate.
ORCHESTRATOR_CHECK_FN = "_check_kanban_orchestrator_mode"
UPSTREAM_CHECK_FN = "_check_kanban_mode"
WORKER_CHECK_FN = "_check_kanban_worker_mode"

#: The names in upstream's registration block this patch reasons about.
TOOL_TABLE = "_TOOLS"
ORCHESTRATOR_SET = "_ORCHESTRATOR_TOOLS"
GATE_LOCAL = "_gate"
LOOP_TOOL_NAME = "_name"

#: Positions inside a ``_TOOLS`` row.
ROW_NAME, ROW_SCHEMA, ROW_HANDLER = 0, 1, 2
ROW_MIN_LEN = 3

# Handler name per tool. Once part of the anchor text; now an expectation on the
# tool's row in the table, so that a renamed handler fails with "row ... names
# handler X where Y was expected" rather than disappearing into a "found 0".
HANDLERS = {
    "kanban_complete": "_handle_complete",
    "kanban_block": "_handle_block",
    "kanban_heartbeat": "_handle_heartbeat",
    "kanban_link": "_handle_link",
    "kanban_attach": "_handle_attach",
    "kanban_attach_url": "_handle_attach_url",
    "kanban_attachments": "_handle_attachments",
    "kanban_request_review": "_handle_request_review",
    "kanban_request_changes": "_handle_request_changes",
}


def gate_override(indent: str) -> str:
    """The statement spliced after ``_gate = ...`` inside the registration loop."""
    return (
        f"{indent}# kube-agents patch: see tools/kanban_worker_tools.py\n"
        f"{indent}if {LOOP_TOOL_NAME} in _WORKER_ONLY_TOOLS:\n"
        f"{indent}    {GATE_LOCAL} = {WORKER_CHECK_FN}\n"
    )


def check_handler_mapping() -> None:
    """Refuse to run if this applier and kanban_worker_tools.py have drifted."""
    missing = set(WORKER_ONLY_TOOLS) - set(HANDLERS)
    if missing:
        raise SystemExit(
            "kanban_worker_tools patch: no handler mapping for "
            f"{', '.join(sorted(missing))} — kanban_worker_tools.py and this "
            "applier have drifted apart."
        )


def _is_name(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def expect_tool_rows(patch: patchlib.Patch, table: patchlib.Assignment) -> None:
    """Assert ``_TOOLS`` still carries one row per worker-only tool, as expected.

    The row is what the per-tool ``registry.register`` locator used to be: the
    statement that says which schema and which handler ``kanban_complete`` is.
    A row that names a different handler means the gate would be swapped on a
    tool that no longer does what this patch thinks it does.
    """
    if not isinstance(table.node.value, ast.Tuple):
        raise patch._fail(
            f"{TOOL_TABLE} is {patchlib._render(table.node.value)}, not a "
            f"tuple of rows. {patch.note}"
        )
    for tool in WORKER_ONLY_TOOLS:
        rows = [
            row
            for row in table.node.value.elts
            if isinstance(row, ast.Tuple)
            and len(row.elts) >= ROW_MIN_LEN
            and isinstance(row.elts[ROW_NAME], ast.Constant)
            and row.elts[ROW_NAME].value == tool
        ]
        if len(rows) != 1:
            raise patch._fail(
                f"expected 1 {TOOL_TABLE} row for {tool}, found {len(rows)}. "
                f"{patch.note}"
            )
        row = rows[0]
        schema = f"{tool.upper()}_SCHEMA"
        if not _is_name(row.elts[ROW_SCHEMA], schema):
            raise patch._fail(
                f"the {tool} row names schema "
                f"{patchlib._render(row.elts[ROW_SCHEMA])} where {schema} was "
                f"expected. {patch.note}"
            )
        if not _is_name(row.elts[ROW_HANDLER], HANDLERS[tool]):
            raise patch._fail(
                f"the {tool} row names handler "
                f"{patchlib._render(row.elts[ROW_HANDLER])} where "
                f"{HANDLERS[tool]} was expected. {patch.note}"
            )


def expect_not_orchestrator(patch: patchlib.Patch, chosen: patchlib.Assignment) -> None:
    """Assert upstream does not already route any worker-only tool as orchestrator-only."""
    present = {
        node.value
        for node in ast.walk(chosen.node.value)
        if isinstance(node, ast.Constant)
    }
    clash = [tool for tool in WORKER_ONLY_TOOLS if tool in present]
    if clash:
        raise patch._fail(
            f"{ORCHESTRATOR_SET} now lists {', '.join(clash)}, which this patch "
            f"gates as worker-only. {patch.note}"
        )


def expect_gate_choice(patch: patchlib.Patch, gate: patchlib.Assignment) -> None:
    """Assert ``_gate`` is still the conditional between upstream's two gates,
    chosen on the loop variable the inserted override reads."""
    value = gate.node.value
    if not (
        isinstance(value, ast.IfExp)
        and _is_name(value.body, ORCHESTRATOR_CHECK_FN)
        and _is_name(value.orelse, UPSTREAM_CHECK_FN)
    ):
        raise patch._fail(
            f"{GATE_LOCAL} is {patchlib._render(value)} where "
            f"'{ORCHESTRATOR_CHECK_FN} if ... else {UPSTREAM_CHECK_FN}' was "
            f"expected. {patch.note}"
        )
    # The override is ``if _name in _WORKER_ONLY_TOOLS``; pin that the choice
    # upstream makes reads the same loop variable, or a renamed loop target
    # would pass the two arms above and ship a NameError at import.
    test = value.test
    if not (
        isinstance(test, ast.Compare)
        and _is_name(test.left, LOOP_TOOL_NAME)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.In)
    ):
        raise patch._fail(
            f"{GATE_LOCAL} chooses on {patchlib._render(test)} where "
            f"'{LOOP_TOOL_NAME} in ...' was expected; the inserted override "
            f"reads {LOOP_TOOL_NAME}. {patch.note}"
        )


def apply(root: Path) -> None:
    """Apply every edit under ``root``, or raise SystemExit with the reason."""
    check_handler_mapping()
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_worker_tools")
    # Neither edit consumes its anchor, so anchor-counting alone cannot catch a
    # re-run; refuse explicitly rather than stack a second import and override.
    patch.refuse_if_patched(WORKER_CHECK_FN)

    expect_tool_rows(patch, patch.find_assign(TOOL_TABLE, label="tool table"))
    expect_not_orchestrator(
        patch, patch.find_assign(ORCHESTRATOR_SET, label="orchestrator set")
    )

    gate = patch.find_assign(GATE_LOCAL, label="registration gate")
    expect_gate_choice(patch, gate)
    patch.insert(gate.after, gate_override(gate.indent))

    site = patch.find_def(IMPORT_AFTER, label="worker-gate import site")
    patch.insert(site.after, IMPORT_BLOCK)

    patch.commit(f"1 import + 1 gate override ({len(WORKER_ONLY_TOOLS)} tools)")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
