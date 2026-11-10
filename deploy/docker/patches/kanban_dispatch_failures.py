"""Count the kanban dispatcher's failed ticks per board, on the chat_metrics registry.

Installed as ``gateway/kanban_dispatch_failures.py`` and called from the two
places ``apply_kanban_dispatch_failures.py`` patches into
``gateway/kanban_watchers_dispatcher.py``'s ``tick_once_for_board``: after
``dispatch_once`` returns (a good tick, which ends the board's streak) and
after the ``tick failed on board`` log line (a failed tick, which adds one).
The registry renders the streak as ``kubeagents_kanban_dispatcher_failed_ticks``
on the listener the operator's usage poller reads, and the poller sets the
PlatformAgent's ``KanbanDispatcherFailing`` condition from it.

The corrupt-board path upstream quarantines is not counted: it logs its own
error and pauses the board, and a quarantined board's skipped ticks leave its
streak as it was.

Never raises: the dispatcher must run as upstream ships it when the plugin is
absent. The registry is the one ``chat_turn_metrics`` finds, reached the same
lazy way.
"""

from __future__ import annotations

import logging
from typing import Optional

from gateway import chat_turn_metrics

LOGGER = logging.getLogger("kube-agents.kanban-dispatch-failures")


def record_tick(board: str, exc: Optional[BaseException] = None) -> None:
    """Record one dispatcher tick on ``board``: failed when ``exc`` is set. Never raises."""
    try:
        registry = chat_turn_metrics._load_registry()
        if registry is not None:
            registry.record_dispatch_tick(str(board), type(exc).__name__ if exc is not None else "")
    except Exception:  # noqa: BLE001 - a metric must never fail the dispatcher's tick
        LOGGER.debug("dispatcher tick not recorded", exc_info=True)
