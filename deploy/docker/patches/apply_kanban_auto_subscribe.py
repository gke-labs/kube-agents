#!/usr/bin/env python3
"""Wire tools/kanban_auto_subscribe.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit
plus an import trailer: the ``kanban_create`` handler gains a call to
``maybe_inherit_worker_subscriptions`` between ``kb.create_task`` returning and
the handler's ``return``, alongside the ``_maybe_auto_subscribe`` attempt it
complements — the session-context path covers in-chat creators, this call
covers dispatcher-spawned workers, and between them every creator with a route
back to a chat thread passes it on.

The anchor is the line that reads the created card back
(``landed = _fields(kb.get_task(conn, new_tid), _CREATED_FIELDS)``), so the
child exists and upstream's own inheritance has already run; ``INSERT OR
IGNORE`` makes the overlap free. Since v2026.9.14 upstream's
``_maybe_auto_subscribe`` is called inline in the ``return`` rather than bound
to a local first, so the hook lands one line above it instead of one below; a
worker process has no session context for that call to act on, so the order of
the two is immaterial.

**Largely superseded upstream.** ``create_task`` takes
``creator_task_id=<HERMES_KANBAN_TASK>`` since upstream commit 3b7ff435fd
(2026-09-07, "preserve durable origins for worker-created tasks") and copies
the creator card's ``kanban_notify_subs`` rows to the child inside creation's
own transaction (``kanban_db_graph.inherit_creator_origin`` ->
``_inherit_notify_subs``). ``verify_kanban_auto_subscribe.py`` run against the
unpatched v2026.9.14 base passes every incident check; what it still fails is
the idempotent re-create of a card that was born outside any worker (upstream
returns the existing id before it opens ``write_txn``, so nothing inherits),
plus the wiring check itself. The applier is kept green so the Dockerfile step
can be retired deliberately rather than by a build break.

Why the change was needed is documented in the module docstring of
``deploy/docker/patches/kanban_auto_subscribe.py``. Usage::

    python3 apply_kanban_auto_subscribe.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "tools/kanban_tools.py"

ANCHOR = "        landed = _fields(kb.get_task(conn, new_tid), _CREATED_FIELDS)\n"

PATCHED = ANCHOR + (
    "        # kube-agents patch: a worker's child card inherits the chat\n"
    "        # subscription of the card the worker is running, so the\n"
    "        # user's thread follows fanned-out work without the worker\n"
    "        # having to remember a propagation script.\n"
    "        # See tools/kanban_auto_subscribe.py.\n"
    "        _kanban_inherit_worker_subs(conn, new_tid)\n"
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
