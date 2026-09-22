#!/usr/bin/env python3
"""Wire tools/kanban_event_routing.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit
plus an import trailer: ``_resolve_notify_target`` gains a single call that
rewrites the three destination values it is about to hand to
``kanban_db_notify.add_notify_sub``, so a card filed from an event-triage
session is addressed to the chat thread that raised the alert instead of to
the ``api_server`` origin no notifier can deliver to.

Since v2026.9.14 upstream builds the subscription's kwargs in
``_resolve_notify_target`` (split out of ``_maybe_auto_subscribe``, which now
only writes them). The anchor sits after ``thread_id`` is read there and after
the ``if not platform or not chat_id`` early return, which an event session
passes (the chokepoint sets both). Placing it there lets one call site rewrite
all three values before anything downstream reads them: ``delivery_metadata``
takes ``thread_id`` from the rewritten value, and ``delivery_mode`` is computed
in the ``return`` as ``platform != "tui"``, which neither the ``api_server``
origin nor the chat platform substituted for it changes.

Upstream's own answer to an ``api_server`` subscription is different and
complementary: ``add_notify_sub`` defaults such rows to ``notify+wake`` so the
wake self-post is the delivery. That wakes an agent session, not the human's
chat thread; this patch is still what makes the report land under the alert.

Why the change is needed is documented in the module docstring of
``deploy/docker/patches/kanban_event_routing.py``. Usage::

    python3 apply_kanban_event_routing.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

RELATIVE = "tools/kanban_tools.py"

ANCHOR = (
    '    chat_type = env("HERMES_SESSION_CHAT_TYPE", "") or None\n'
    '    thread_id = env("HERMES_SESSION_THREAD_ID", "") or None\n'
)

PATCHED = ANCHOR + (
    "    # kube-agents patch: an event-triage turn reaches here with the\n"
    "    # api_server chokepoint's values — platform='api_server' and the\n"
    "    # session id in chat_id — so the row written from these is well-formed\n"
    "    # and undeliverable. The watcher already recorded the alert's real\n"
    "    # chat route under that session id; substitute it so the card's\n"
    "    # completion reaches the thread a human is reading.\n"
    "    # See tools/kanban_event_routing.py.\n"
    "    platform, chat_id, thread_id = _kanban_event_route(\n"
    "        platform, chat_id, thread_id\n"
    "    )\n"
)

# Appended rather than inserted: the name is resolved when the tool handler
# runs, long after the module finishes importing. Same placement the other
# kanban patches use.
TRAILER = (
    "\n\n# kube-agents patch: see tools/kanban_event_routing.py\n"
    "from tools.kanban_event_routing import (  # noqa: E402\n"
    "    resolve_chat_route as _kanban_event_route,\n"
    ")\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_event_routing")
    # The patched text keeps the anchor (the call is appended after it), so
    # anchor-counting alone cannot catch a re-run. Refuse explicitly rather
    # than stack a second call and a second trailer import.
    patch.refuse_if_patched("_kanban_event_route(")
    patch.substitute(ANCHOR, PATCHED)
    patch.append(TRAILER)
    patch.commit("1 anchor")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
