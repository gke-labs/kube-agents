#!/usr/bin/env python3
"""Wire hermes_cli/kanban_origin.py into the board's insert path and the CLI create.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``, in the latency
block after ``apply_kanban_priority.py``. Three trailers, one per file, each
held in memory and compiled before any is written:

``hermes_cli/kanban_db_graph.py``

    1. ``inherit_creator_origin`` is rebound to ``wrap_inherit_creator_origin``.
       Upstream calls it inside the insert's ``write_txn`` from both inserters,
       ``kanban_db.create_task`` (a function-local import, read at call time)
       and ``_insert_decomposed_child`` (a module global, read at call time),
       so the stamp lands in the insert's own transaction for every card. The
       applier asserts both call sites are still there.

``hermes_cli/kanban_db.py``

    2. ``create_task`` is rebound to ``wrap_create_task``, which resolves a
       top-level card's origin before the transaction opens. Every caller reads
       it as ``kb.create_task`` (a module attribute), so the rebinding reaches
       the tool, the CLI, swarm and the dashboard route alike.

``hermes_cli/kanban.py``

    3. ``_cmd_create`` and ``_cmd_swarm`` are rebound to ``wrap_cli_create``
       and put back into ``_HANDLERS``, which ``kanban_command`` and so
       ``run_slash`` dispatch through. That marks the CLI create and swarm
       paths, whose ``--created-by`` is an argument and so never reads as the
       dashboard's. No argument is added: the create parser rejects
       ``--origin``.

Why is documented in the module docstring of
``deploy/docker/patches/kanban_origin.py``. Usage::

    python3 apply_kanban_origin.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

PREFIX = "kanban_origin"

GRAPH_RELATIVE = "hermes_cli/kanban_db_graph.py"
DB_RELATIVE = "hermes_cli/kanban_db.py"
CLI_RELATIVE = "hermes_cli/kanban.py"

# --- 1. the stamp, inside the insert's transaction ---------------------------
INHERIT = "inherit_creator_origin"
#: The two inserters and the call each makes inside its transaction.
INSERT_SITES = (
    (DB_RELATIVE, "create_task", "inherit_creator_origin(conn, task_id, creator_task_id, created_at=now)"),
    (GRAPH_RELATIVE, "_insert_decomposed_child", "inherit_creator_origin(conn, new_id, root_id, created_at=now)"),
)
GRAPH_MARKER = "_kanban_origin_wrap_inherit("
GRAPH_TRAILER = (
    "\n\n# kube-agents patch: stamp a new card's origin in the insert's own\n"
    "# transaction. See hermes_cli/kanban_origin.py.\n"
    "from hermes_cli.kanban_origin import (  # noqa: E402\n"
    "    wrap_inherit_creator_origin as _kanban_origin_wrap_inherit,\n"
    ")\n"
    "inherit_creator_origin = _kanban_origin_wrap_inherit(inherit_creator_origin)\n"
)

# --- 2. a top-level card's origin, resolved before the transaction ------------
CREATE = "create_task"
DB_MARKER = "_kanban_origin_wrap_create_task("
DB_TRAILER = (
    "\n\n# kube-agents patch: resolve a new card's origin before the insert's\n"
    "# transaction. See hermes_cli/kanban_origin.py.\n"
    "from hermes_cli.kanban_origin import (  # noqa: E402\n"
    "    wrap_create_task as _kanban_origin_wrap_create_task,\n"
    ")\n"
    "create_task = _kanban_origin_wrap_create_task(create_task)\n"
)

# --- 3. the CLI create path ---------------------------------------------------
CLI_CREATE = "_cmd_create"
CLI_SWARM = "_cmd_swarm"
HANDLERS = "_HANDLERS"
HANDLERS_ENTRY = '"create": _cmd_create'
HANDLERS_SWARM_ENTRY = '"swarm": _cmd_swarm'
CLI_MARKER = "_kanban_origin_wrap_cli_create("
CLI_TRAILER = (
    "\n\n# kube-agents patch: mark the CLI create and swarm paths, whose\n"
    "# --created-by is an argument, not the dashboard. See hermes_cli/kanban_origin.py.\n"
    "from hermes_cli.kanban_origin import (  # noqa: E402\n"
    "    wrap_cli_create as _kanban_origin_wrap_cli_create,\n"
    ")\n"
    "_cmd_create = _kanban_origin_wrap_cli_create(_cmd_create)\n"
    '_HANDLERS["create"] = _cmd_create\n'
    "_cmd_swarm = _kanban_origin_wrap_cli_create(_cmd_swarm)\n"
    '_HANDLERS["swarm"] = _cmd_swarm\n'
)


def apply(root: Path) -> None:
    """Apply every edit under ``root``, or raise SystemExit with the reason.

    Nothing is written until every locator in all three files has matched and
    all three results compile.
    """
    patches = {
        relative: patchlib.Patch(root, relative, prefix=PREFIX)
        for relative in (GRAPH_RELATIVE, DB_RELATIVE, CLI_RELATIVE)
    }
    for relative, marker in ((GRAPH_RELATIVE, GRAPH_MARKER), (DB_RELATIVE, DB_MARKER), (CLI_RELATIVE, CLI_MARKER)):
        patches[relative].refuse_if_patched(marker)

    graph = patches[GRAPH_RELATIVE]
    graph.find_def(INHERIT, label="creator-origin inheritance")
    for relative, function, call in INSERT_SITES:
        patch = patches[relative]
        site = patch.find_def(function, label="card inserter")
        inside = patch.source[site.start : site.end].count(call)
        if inside != 1:
            raise SystemExit(
                f"{PREFIX} patch: {relative}: expected {call!r} once inside "
                f"{function}(), found {inside}: the stamp would miss that inserter. {patch.note}"
            )
    graph.append(GRAPH_TRAILER)

    db = patches[DB_RELATIVE]
    db.append(DB_TRAILER)

    cli = patches[CLI_RELATIVE]
    cli.find_def(CLI_CREATE, label="CLI create command")
    cli.find_def(CLI_SWARM, label="CLI swarm command")
    handlers = cli.find_assign(HANDLERS, label="CLI command table")
    for entry in (HANDLERS_ENTRY, HANDLERS_SWARM_ENTRY):
        if entry not in handlers.value_text:
            raise SystemExit(
                f"{PREFIX} patch: {CLI_RELATIVE}: {HANDLERS} no longer maps "
                f"{entry!r}. {cli.note}"
            )
    cli.append(CLI_TRAILER)

    for patch in patches.values():
        try:
            compile(patch.source, patch.relative, "exec")
        except SyntaxError as exc:
            raise SystemExit(
                f"{PREFIX} patch: {patch.relative} no longer parses after patching: {exc}"
            )
    graph.commit("trailer")
    db.commit("trailer")
    cli.commit("trailer")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
