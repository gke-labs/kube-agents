#!/usr/bin/env python3
"""Wire gateway/kanban_chat_notify.py into the Hermes kanban notifier.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``. One anchored edit
and one trailer in ``gateway/kanban_watchers_notifier.py``. Why the change is
needed (a card subscribed to a chat platform the A2A gateway holds is skipped
forever under ``spec.mode: next``) is in the module docstring of
``deploy/docker/patches/kanban_chat_notify.py``; this file documents where the
edits land.

The anchor is ``_Collector``'s ``active_platforms``: the coarse filter
``_claim_for_sub`` applies before anything else, so a routed platform has to be
in it or its subscriptions are never looked at.

The trailer rebinds the module-level ``_adapter_for_subscription`` to a wrapper
that falls back to the stand-in adapter. A rebinding rather than an anchor at
each call site, because both callers (``_claim_for_sub``'s authorization and
``_KanbanNotification.deliver``) look the name up in module globals at call
time, and the wrapper only acts when upstream returned no adapter, so every
other subscription resolves exactly as before.

Ordering. Runs after ``apply_kanban_notify_delivery.py``, which rewrites the
claim in ``_claim_for_sub`` but leaves the ``active_platforms`` assignment and
the function this trailer wraps untouched. Appends compose.

Usage::

    python3 apply_kanban_chat_notify.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

NOTIFIER_RELATIVE = "gateway/kanban_watchers_notifier.py"

#: Nesting depth of a method body on ``_Collector``.
METHOD_INDENT = " " * 8

ACTIVE_ANCHOR = (
    f"{METHOD_INDENT}self.active_platforms = _platform_names(runner.adapters).union(\n"
    f"{METHOD_INDENT}    *(_platform_names(m) for m in self.profile_adapters.values()))\n"
)

ACTIVE_PATCHED = (
    f"{METHOD_INDENT}# kube-agents patch: a platform the A2A gateway holds counts as\n"
    f"{METHOD_INDENT}# served. See gateway/kanban_chat_notify.py.\n"
    f"{METHOD_INDENT}self.active_platforms = _kage_chat_notify_active(_platform_names(runner.adapters).union(\n"
    f"{METHOD_INDENT}    *(_platform_names(m) for m in self.profile_adapters.values())))\n"
)

TRAILER = (
    "\n\n# kube-agents patch: see gateway/kanban_chat_notify.py\n"
    "from gateway.kanban_chat_notify import (  # noqa: E402\n"
    "    active_platforms as _kage_chat_notify_active,\n"
    "    resolve as _kage_chat_notify_resolve,\n"
    ")\n"
    "\n"
    "_kage_upstream_adapter_for_subscription = _adapter_for_subscription\n"
    "\n"
    "\n"
    "def _adapter_for_subscription(runner, platform, sub, owner_profile):  # noqa: F811\n"
    "    return _kage_chat_notify_resolve(\n"
    "        runner, platform, _kage_upstream_adapter_for_subscription(runner, platform, sub, owner_profile),\n"
    "    )\n"
)

SENTINELS = (
    "_kage_chat_notify_active(",
    "from gateway.kanban_chat_notify import",
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    notifier = patchlib.Patch(root, NOTIFIER_RELATIVE, prefix="kanban_chat_notify")
    notifier.refuse_if_patched(*SENTINELS)
    notifier.substitute(ACTIVE_ANCHOR, ACTIVE_PATCHED, label="active platforms")
    notifier.append(TRAILER)
    notifier.commit("1 anchor, 1 trailer")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
