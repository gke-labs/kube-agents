"""Enforce ``PRAGMA synchronous=FULL`` on Linux, not only on macOS.

One anchored edit in ``hermes_state.py``: the platform gate at the top of
``_enforce_macos_synchronous_full`` returns early on anything but Darwin, so on
Linux a WAL-mode ``state.db`` or ``kanban.db`` runs at whatever ``synchronous``
the connection or a prior caller left it — the WAL default is NORMAL, which does
not fsync on every commit. The function's own docstring describes the failure
that setting allows: a checkpoint racing process termination leaves half-written
btree pages in the main file. That is the corruption #610 found on a gVisor
gofer mount, where the pin to DELETE (renderConfigYAML in the operator) is the
fix and this is the belt for a database that stays in WAL — an install without a
runtime class, or a file the conversion could not reach.

Edited inside that one def's span only. ``_apply_macos_checkpoint_barrier``
carries the same gate for ``checkpoint_fullfsync``, which is an F_FULLFSYNC
barrier that exists on macOS alone, and stays as it is.

On the pinned base an unpatched WAL connection already reports ``synchronous``
= 2 (SQLite's compiled default), so on today's image this guards against a base
or caller that sets NORMAL rather than changing observed behaviour; the
verifier proves the patched function restores FULL after NORMAL has been set.
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

STATE_RELATIVE = "hermes_state.py"

# The def whose gate is widened. Located by AST, so the edit cannot land in
# _apply_macos_checkpoint_barrier, which spells its gate the same way.
ENFORCE_DEF = "_enforce_macos_synchronous_full"

# The gate as upstream writes it, without its indentation, so the replacement
# keeps whatever indentation the file has.
DARWIN_GATE = 'if sys.platform != "darwin":\n'
LINUX_GATE = (
    'if sys.platform not in ("darwin", "linux"):'
    "  # kube-agents patch: state-db-synchronous-full\n"
)

MARKER = "kube-agents patch: state-db-synchronous-full"


def apply(root: Path) -> None:
    patch = patchlib.Patch(root, STATE_RELATIVE, prefix="state-db-synchronous-full")
    patch.refuse_if_patched(MARKER)
    definition = patch.find_def(ENFORCE_DEF, label="synchronous=FULL enforcement")
    body = patch.source[definition.start : definition.end]
    found = body.count(DARWIN_GATE)
    if found != 1:
        raise SystemExit(
            f"state-db-synchronous-full patch: {STATE_RELATIVE}: expected 1 "
            f"platform gate in {ENFORCE_DEF}(), found {found}. {patch.note}"
        )
    offset = definition.start + body.index(DARWIN_GATE)
    patch.splice(offset, offset + len(DARWIN_GATE), LINUX_GATE)
    patch.commit("synchronous=FULL enforced on linux as well as darwin")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
