#!/usr/bin/env python3
"""Wire tools/kanban_event_routing.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit
plus an import trailer: ``_maybe_auto_subscribe`` gains a single call that
rewrites the three destination values it is about to write into
``kanban_notify_subs``, so a card filed from an event-triage session is
addressed to the chat thread that raised the alert instead of to the
``api_server`` origin no notifier can deliver to.

The anchor sits after ``thread_id`` is read and after the
``if not platform or not chat_id`` early return, which an event session passes
(the chokepoint sets both). Placing it there lets one call site rewrite all
three values without a later read overwriting one of them.

``is_gateway_session`` and ``delivery_mode`` are computed from ``platform``
just above the insert and are deliberately left alone: both branch on
``platform != "tui"``, and neither the ``api_server`` origin nor the chat
platform substituted for it is ``"tui"``, so the substitution cannot change
what they resolve to.

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
    '        is_gateway_session = platform != "tui"\n'
    '        chat_type = get_session_env("HERMES_SESSION_CHAT_TYPE", "") or None\n'
    '        delivery_mode = "notify+wake" if is_gateway_session else None\n'
    '        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None\n'
)

PATCHED = ANCHOR + (
    "        # kube-agents patch: an event-triage turn reaches here with the\n"
    "        # api_server chokepoint's values — platform='api_server' and the\n"
    "        # session id in chat_id — so the row written below is well-formed\n"
    "        # and undeliverable. The watcher already recorded the alert's real\n"
    "        # chat route under that session id; substitute it so the card's\n"
    "        # completion reaches the thread a human is reading.\n"
    "        # See tools/kanban_event_routing.py.\n"
    "        platform, chat_id, thread_id = _kanban_event_route(\n"
    "            platform, chat_id, thread_id\n"
    "        )\n"
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
