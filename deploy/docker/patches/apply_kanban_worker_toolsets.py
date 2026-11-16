#!/usr/bin/env python3
"""Wire tools/kanban_worker_toolsets.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit
in ``hermes_cli/kanban_db_dispatch.py`` plus an import trailer:
``_resolve_worker_cli_toolsets`` hands the resolution to the module, which runs
this same function in a child process. In that child the module's flag is set,
so the function falls through to upstream's body; a multiplexed gateway does too.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_worker_toolsets.py`` (#3937). Usage::

    python3 apply_kanban_worker_toolsets.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "hermes_cli/kanban_db_dispatch.py"

# The head of upstream's body, right after the docstring.
BODY_ANCHOR = (
    "    if not hermes_home:\n"
    "        return None\n"
    "    try:\n"
    "        from agent.secret_scope import (\n"
)
BODY_PATCHED = (
    "    if not hermes_home:\n"
    "        return None\n"
    "    # kube-agents patch: resolve in a child process, so the profile's plugins\n"
    "    # are never imported or registered in the gateway (#3937). The child runs\n"
    "    # this function with the flag set and falls through to upstream's body, as\n"
    "    # does a multiplexed gateway. See tools/kanban_worker_toolsets.py.\n"
    "    if not _kanban_worker_toolsets.keep_in_process():\n"
    "        return _kanban_worker_toolsets.resolve_worker_toolsets(hermes_home)\n"
    "    try:\n"
    "        from agent.secret_scope import (\n"
)

# Appended rather than inserted: the name is resolved at spawn time, long after
# the module finishes importing.
TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_worker_toolsets.py\n"
    "from tools import kanban_worker_toolsets as _kanban_worker_toolsets  # noqa: E402\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_worker_toolsets")
    patch.refuse_if_patched("_kanban_worker_toolsets.resolve_worker_toolsets(hermes_home)")
    patch.substitute(BODY_ANCHOR, BODY_PATCHED, label="worker toolset resolution")
    patch.append(TRAILER)
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
