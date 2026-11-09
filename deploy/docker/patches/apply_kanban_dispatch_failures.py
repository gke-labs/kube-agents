#!/usr/bin/env python3
"""Wire gateway/kanban_dispatch_failures.py into the dispatcher's per-board tick.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree, after
``kanban_dispatch_failures.py`` has been copied to ``gateway/`` and after
``chat_turn_metrics.py``, whose registry lookup it uses. One file, two sites,
one import, in ``gateway/kanban_watchers_dispatcher.py``'s
``tick_once_for_board``:

1. The good tick: ``dispatch_once``'s result is kept, the tick is recorded,
   and the result is returned, so the board's streak ends.
2. The failed tick: right after ``logger.exception("kanban dispatcher: tick
   failed on board %s", slug)``, the tick is recorded with the exception, so
   the board's streak grows. The corrupt-board branch above it returns first
   and is not counted.

Each site calls ``_kage_dispatch_failures.record_tick``, which never raises
and records nothing when the ``chat_metrics`` plugin is absent. Anchors are
derived against v2026.9.14; no other patch edits this file.

Usage::

    python3 apply_kanban_dispatch_failures.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

PREFIX = "kanban-dispatch-failures"
MARKER = "kube-agents patch: kanban-dispatch-failures"
DEFAULT_HERMES_ROOT = "/opt/hermes"

DISPATCHER = "gateway/kanban_watchers_dispatcher.py"

IMPORT_ANCHOR = "\nfrom gateway.kanban_watchers_common import _board_slugs, _positive_int_setting, logger\n"
IMPORT_PATCHED = IMPORT_ANCHOR + (
    f"from gateway import kanban_dispatch_failures as _kage_dispatch_failures  # noqa: E402  {MARKER}\n"
)

GOOD_TICK_ANCHOR = "            return _kbd().dispatch_once(conn, board=slug, **kwargs)\n"
GOOD_TICK_PATCHED = (
    "            _kage_dispatch_result = _kbd().dispatch_once(conn, board=slug, **kwargs)\n"
    f"            _kage_dispatch_failures.record_tick(slug)  # {MARKER}\n"
    "            return _kage_dispatch_result\n"
)

FAILED_TICK_ANCHOR = '            logger.exception("kanban dispatcher: tick failed on board %s", slug)\n'
FAILED_TICK_PATCHED = FAILED_TICK_ANCHOR + f"            _kage_dispatch_failures.record_tick(slug, exc)  # {MARKER}\n"


def apply(root: Path) -> None:
    dispatcher = patchlib.Patch(root, DISPATCHER, prefix=PREFIX)
    dispatcher.refuse_if_patched(MARKER)
    dispatcher.substitute(IMPORT_ANCHOR, IMPORT_PATCHED, label="dispatcher import")
    dispatcher.substitute(GOOD_TICK_ANCHOR, GOOD_TICK_PATCHED, label="good tick")
    dispatcher.substitute(FAILED_TICK_ANCHOR, FAILED_TICK_PATCHED, label="failed tick")
    dispatcher.commit("2 anchors, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HERMES_ROOT))
