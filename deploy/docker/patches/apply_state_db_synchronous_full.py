"""Enforce ``PRAGMA synchronous=FULL`` on Linux, not only on macOS.

One anchored edit in ``hermes_state_wal.py`` (the WAL half of the
``hermes_state`` split): ``_enforce_macos_synchronous_full`` delegates to
``_darwin_pragma``, a helper that runs its PRAGMA on Darwin and is a no-op
everywhere else, so on Linux a WAL-mode ``state.db`` or ``kanban.db`` runs at
whatever ``synchronous`` the connection or a prior caller left it — the WAL
default is NORMAL, which does not fsync on every commit. The function's own
docstring describes the failure that setting allows: a checkpoint racing
process termination leaves half-written btree pages in the main file. That is
the corruption #610 found on a gVisor gofer mount, where the pin to DELETE
(renderConfigYAML in the operator) is the fix and this is the belt for a
database that stays in WAL — an install without a runtime class, or a file the
conversion could not reach.

The edit lands inside that one def's span only: a Linux branch runs the same
PRAGMA directly, ahead of the unchanged Darwin delegation, so macOS behaviour is
untouched. ``_darwin_pragma`` itself is deliberately not widened — its other
caller, ``_apply_macos_checkpoint_barrier``, sets ``checkpoint_fullfsync``,
which is an F_FULLFSYNC barrier that exists on macOS alone.

On an unpatched image a fresh connection reports ``synchronous`` = 2 (SQLite's
compiled default) but the enforcement leaves a connection set to NORMAL at
NORMAL on Linux; the verifier proves the patched function restores FULL.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

STATE_RELATIVE = "hermes_state_wal.py"

# The def whose body gains the Linux branch. Located by AST, so the edit cannot
# land in _apply_macos_checkpoint_barrier, which delegates the same way.
ENFORCE_DEF = "_enforce_macos_synchronous_full"

# The delegation as upstream writes it, without its indentation, so the
# replacement keeps whatever indentation the file has. No trailing newline:
# it is the def's last line, and a def's AST span ends at its last character.
DELEGATE_CALL = '_darwin_pragma(conn, "PRAGMA synchronous=FULL")'

MARKER = "kube-agents patch: state-db-synchronous-full"

# The Linux branch, one level of indentation per nested line; the def's own
# body indentation is prepended at apply time. ``contextlib``, ``sqlite3`` and
# ``sys`` are the names ``_darwin_pragma`` already uses in this module.
LINUX_BRANCH_LINES = (
    f'if sys.platform == "linux":  # {MARKER}',
    "    with contextlib.suppress(sqlite3.OperationalError):",
    '        conn.execute("PRAGMA synchronous=FULL")',
    "    return",
)


def linux_branch(indent: str) -> str:
    """The inserted branch at the def's body indentation."""
    return "".join(indent + line + "\n" for line in LINUX_BRANCH_LINES)


def apply(root: Path) -> None:
    patch = patchlib.Patch(root, STATE_RELATIVE, prefix="state-db-synchronous-full")
    patch.refuse_if_patched(MARKER)
    definition = patch.find_def(ENFORCE_DEF, label="synchronous=FULL enforcement")
    indent = definition.body_indent
    # Bounded by the preceding line break, so the line is matched whole and a
    # deeper-indented lookalike cannot stand in for it.
    anchor = "\n" + indent + DELEGATE_CALL
    body = "\n" + patch.source[definition.start : definition.end]
    found = body.count(anchor)
    if found != 1:
        raise SystemExit(
            f"state-db-synchronous-full patch: {STATE_RELATIVE}: expected 1 "
            f"synchronous=FULL delegation in {ENFORCE_DEF}(), found {found}. {patch.note}"
        )
    offset = definition.start + body.index(anchor)
    patch.splice(offset, offset, linux_branch(indent))
    patch.commit("synchronous=FULL enforced on linux as well as darwin")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
