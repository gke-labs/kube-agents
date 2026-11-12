#!/usr/bin/env python3
"""Wire gateway/kanban_dispatch_failures.py into the dispatcher's per-board tick.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree, after
``kanban_dispatch_failures.py`` has been copied to ``gateway/`` and after
``chat_turn_metrics.py``, whose registry lookup it uses. One file, eight sites,
one import, in ``gateway/kanban_watchers_dispatcher.py``'s
``tick_once_for_board``:

1. The good tick: ``dispatch_once``'s result is kept, the tick is recorded,
   and the result is returned, so the board's streak ends.
2. The failed tick: right after ``logger.exception("kanban dispatcher: tick
   failed on board %s", slug)``, the tick is recorded with the exception, so
   the board's streak grows. The corrupt-board branch above it returns first
   and is not counted.
3. The tick's start, after the quarantine check and before ``connect()``,
   which on a locked board waits the busy timeout once per statement it runs.
4. The tick's end, first in the ``finally`` block, on every way out.
5-8. The same start and end around ``ready_nonempty``'s and
   ``auto_decompose_tick``'s per-board connects, which the loop runs beside the
   tick and which wait on a locked board just as long.

The start and the end let the registry tell a board being tried (a locked
board's tick can take many minutes) from one the dispatcher stopped ticking.
Each site calls a ``_kage_dispatch_failures`` function, which never raises
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

# The tick's start, after the quarantine check (a quarantined board's skipped tick is no try) and
# before connect(), which on a locked board waits the busy timeout once per statement it runs.
TICK_START_ANCHOR = '        kwargs = {k: v for k, v in asdict(self.settings).items() if k != "interval"}\n'
TICK_START_PATCHED = f"        _kage_dispatch_failures.record_tick_start(slug)  # {MARKER}\n" + TICK_START_ANCHOR

# ready_nonempty and auto_decompose_tick connect to each board too, in the same loop iteration as
# the tick, and on a locked board each waits the busy timeout per statement as well (ready_nonempty
# swallows the error unlogged). Marking them as tries keeps a locked board "being tried" for the
# whole iteration, not only during the tick.
READY_START_ANCHOR = "            conn = None\n            try:\n                conn = _kbc().connect(board=slug)\n                if kbd.has_spawnable_ready(conn)"
READY_START_PATCHED = (
    "            conn = None\n"
    f"            _kage_dispatch_failures.record_tick_start(slug)  # {MARKER}\n"
    "            try:\n                conn = _kbc().connect(board=slug)\n                if kbd.has_spawnable_ready(conn)"
)
READY_END_ANCHOR = "            finally:\n                if conn is not None:\n"
READY_END_PATCHED = (
    "            finally:\n"
    f"                _kage_dispatch_failures.record_tick_end(slug)  # {MARKER}\n"
    "                if conn is not None:\n"
)
DECOMPOSE_START_ANCHOR = '                    os.environ["HERMES_KANBAN_BOARD"] = slug\n'
DECOMPOSE_START_PATCHED = DECOMPOSE_START_ANCHOR + f"                    _kage_dispatch_failures.record_tick_start(slug)  # {MARKER}\n"
DECOMPOSE_END_ANCHOR = "                finally:\n                    if prev_env is None:\n"
DECOMPOSE_END_PATCHED = (
    "                finally:\n"
    f"                    _kage_dispatch_failures.record_tick_end(slug)  # {MARKER}\n"
    "                    if prev_env is None:\n"
)

# The tick's end, on every way out (a good tick, a failure, the corrupt-board branch).
TICK_END_ANCHOR = "        finally:\n            if conn is not None:\n"
TICK_END_PATCHED = (
    "        finally:\n"
    f"            _kage_dispatch_failures.record_tick_end(slug)  # {MARKER}\n"
    "            if conn is not None:\n"
)


def apply(root: Path) -> None:
    dispatcher = patchlib.Patch(root, DISPATCHER, prefix=PREFIX)
    dispatcher.refuse_if_patched(MARKER)
    dispatcher.substitute(IMPORT_ANCHOR, IMPORT_PATCHED, label="dispatcher import")
    dispatcher.substitute(GOOD_TICK_ANCHOR, GOOD_TICK_PATCHED, label="good tick")
    dispatcher.substitute(FAILED_TICK_ANCHOR, FAILED_TICK_PATCHED, label="failed tick")
    dispatcher.substitute(TICK_START_ANCHOR, TICK_START_PATCHED, label="tick start")
    dispatcher.substitute(TICK_END_ANCHOR, TICK_END_PATCHED, label="tick end")
    dispatcher.substitute(READY_START_ANCHOR, READY_START_PATCHED, label="ready probe start")
    dispatcher.substitute(READY_END_ANCHOR, READY_END_PATCHED, label="ready probe end")
    dispatcher.substitute(DECOMPOSE_START_ANCHOR, DECOMPOSE_START_PATCHED, label="auto-decompose start")
    dispatcher.substitute(DECOMPOSE_END_ANCHOR, DECOMPOSE_END_PATCHED, label="auto-decompose end")
    dispatcher.commit("8 anchors, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HERMES_ROOT))
