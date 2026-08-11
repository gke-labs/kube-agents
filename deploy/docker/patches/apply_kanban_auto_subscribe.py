#!/usr/bin/env python3
"""Wire tools/kanban_auto_subscribe.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit
plus an import trailer: the ``kanban_create`` handler gains a call to
``maybe_inherit_worker_subscriptions`` immediately after the
``_maybe_auto_subscribe`` attempt it complements — the session-context path
covers in-chat creators, the new call covers dispatcher-spawned workers, and
between them every creator with a route back to a chat thread passes it on.

The anchor sits AFTER ``kb.create_task`` returned, so the child exists and
upstream's own parent-edge inheritance (``_inherit_notify_subs``) has already
run; ``INSERT OR IGNORE`` makes the overlap free when the worker's card is
also a graph parent.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_auto_subscribe.py``. Usage::

    python3 apply_kanban_auto_subscribe.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "tools/kanban_tools.py"

ANCHOR = (
    "            new_task = kb.get_task(conn, new_tid)\n"
    "            subscribed = _maybe_auto_subscribe(conn, new_tid)\n"
)

PATCHED = ANCHOR + (
    "            # kube-agents patch: a worker's child card inherits the chat\n"
    "            # subscription of the card the worker is running, so the\n"
    "            # user's thread follows fanned-out work without the worker\n"
    "            # having to remember a propagation script.\n"
    "            # See tools/kanban_auto_subscribe.py.\n"
    "            _kanban_inherit_worker_subs(conn, new_tid)\n"
)

# Appended rather than inserted: the name is resolved when the tool handler
# runs, long after the module finishes importing. Same placement the other
# kanban patches use.
TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_auto_subscribe.py\n"
    "from tools.kanban_auto_subscribe import (  # noqa: E402\n"
    "    maybe_inherit_worker_subscriptions as _kanban_inherit_worker_subs,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_auto_subscribe")
    # The patched text keeps the anchor (the hook is appended after it), so
    # anchor-counting alone cannot catch a re-run. Refuse explicitly rather
    # than stack a second hook call and a second trailer import.
    patch.refuse_if_patched("_kanban_inherit_worker_subs(conn, new_tid)")
    patch.substitute(ANCHOR, PATCHED)
    patch.append(TRAILER)
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
