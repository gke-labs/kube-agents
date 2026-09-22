#!/usr/bin/env python3
"""Wire tools/kanban_comment_status.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit
plus an import trailer: the ``kanban_comment`` handler's success return grows
the card's status alongside the comment id.

One anchor on purpose. ``tools/kanban_tools.py`` is already carved up by the
other kanban patches — ``kanban_auto_subscribe``, ``kanban_report_format`` and
``kanban_children_settled`` (the ``kanban_create`` handler),
``kanban_result_required`` and ``kanban_children_settled`` (the
``kanban_complete`` handler), ``kanban_worker_tools`` (the registration loop),
``kanban_event_routing`` (``_resolve_notify_target``) and ``cron_run_scope``
elsewhere (``kanban_progress_lines`` left this file at v2026.9.14; its schema
edit lives in ``tools/kanban_tools_schemas.py`` now) — and every anchor added
here is another
way a base image bump breaks the build. The two lines below are inside
``_handle_comment``, which none of those touch, and the schema wording is
deliberately left alone: this patch fixes what the model reads *back*, and
paying a second anchor to also rewrite what it reads *first* is not worth it
when the tool call itself was never the mistake.

Order-independent with respect to every other patch on the file. It shares no
line with any of them and appends its import the way
``apply_kanban_auto_subscribe.py`` appends its own.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_comment_status.py``. Usage::

    python3 apply_kanban_comment_status.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "tools/kanban_tools.py"

# `return _ok(task_id=tid, comment_id=cid)` is already unique in the file, but
# the anchor carries the `kb.add_comment` line above it as well. The point of
# the anchor is not just to be unique — it is to be unique *for the right
# reason*. Pinned to the comment write, an upstream refactor that moves the
# return somewhere else fails the build instead of quietly patching whatever
# inherited the line. Both lines sit inside `with _board(...) as (kb, conn):`
# since v2026.9.14, one indent level shallower than the old try/finally.
ANCHOR = (
    "        cid = kb.add_comment(conn, tid, author=author, body=str(body))\n"
    "        return _ok(task_id=tid, comment_id=cid)\n"
)

# `conn` is still open here — the `with _board(...)` block closes it on the
# way out — and the comment is already committed, because add_comment runs its
# own write_txn. So the lookup cannot cost the caller the comment, and
# delivery_fields returns {} rather than raising if it fails anyway.
PATCHED = (
    "        cid = kb.add_comment(conn, tid, author=author, body=str(body))\n"
    "        # kube-agents patch: a bare ok:true let the model promise a\n"
    "        # delivery the card could not make. Report what the card is\n"
    "        # doing. See tools/kanban_comment_status.py.\n"
    "        return _ok(\n"
    "            task_id=tid,\n"
    "            comment_id=cid,\n"
    "            **_comment_delivery_fields(kb, conn, tid),\n"
    "        )\n"
)

# Appended rather than inserted: the name resolves when the handler runs, long
# after the module finishes importing. Same placement, and the same reason, as
# the trailer in apply_kanban_auto_subscribe.py.
TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_comment_status.py\n"
    "from tools.kanban_comment_status import (  # noqa: E402\n"
    "    delivery_fields as _comment_delivery_fields,\n"
    ")\n"
)

# The patch keeps the anchor's first line, so anchor-counting alone cannot
# distinguish "not yet applied" from "applied twice".
SENTINEL = "_comment_delivery_fields(kb, conn, tid)"


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_comment_status")
    patch.refuse_if_patched(SENTINEL)
    patch.substitute(ANCHOR, PATCHED)
    patch.append(TRAILER)
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
