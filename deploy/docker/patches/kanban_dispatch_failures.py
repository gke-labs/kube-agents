"""Count the kanban dispatcher's failed ticks per board, on the chat_metrics registry.

Installed as ``gateway/kanban_dispatch_failures.py`` and called from the eight
places ``apply_kanban_dispatch_failures.py`` patches into
``gateway/kanban_watchers_dispatcher.py``. In ``tick_once_for_board``: after
``dispatch_once`` returns (a good tick, which ends the board's streak), after
the ``tick failed on board`` log line (a failed tick, which adds one), and at
the tick's start and end. In ``ready_nonempty`` and ``auto_decompose_tick``:
at the start and end of each per-board connect. The starts and ends tell the
registry when a board is being tried, and the tick's start is marked as
the tick, so a tick that runs long (a locked board) shows apart from the other
tries.
The registry renders the streak as ``kubeagents_kanban_dispatcher_failed_ticks``
on the listener the operator's usage poller reads, and the poller sets the
PlatformAgent's ``KanbanDispatcherFailing`` condition from it.

The corrupt-board path upstream quarantines is not counted: it logs its own
error and pauses the board. That tick ends with no result, and the registry
then stops counting the board's other tries, so a quarantine that lasts drops
the board's streak and its stuck state after the stale window.

Never raises: the dispatcher must run as upstream ships it when the plugin is
absent. The registry is the one ``chat_turn_metrics`` finds, reached the same
lazy way.
"""

from __future__ import annotations

import logging
from typing import Optional

from gateway import chat_turn_metrics

LOGGER = logging.getLogger("kube-agents.kanban-dispatch-failures")
# SQLite's lock and busy errors are OperationalError with one of these words in
# the message ("database is locked", "database table is locked", "database is busy").
LOCK_ERROR_CLASS = "OperationalError"
LOCK_ERROR_WORDS = ("locked", "busy")


def is_lock_error(exc: Optional[BaseException]) -> bool:
    """Whether ``exc`` is SQLite's lock or busy error ("database is locked")."""
    if exc is None or type(exc).__name__ != LOCK_ERROR_CLASS:
        return False
    message = str(exc).lower()
    return any(word in message for word in LOCK_ERROR_WORDS)


def record_tick(board: str, exc: Optional[BaseException] = None) -> None:
    """Record one dispatcher tick on ``board``: failed when ``exc`` is set. Never raises."""
    try:
        registry = chat_turn_metrics._load_registry()
        if registry is None:
            return
        error_class = type(exc).__name__ if exc is not None else ""
        if not is_lock_error(exc):
            registry.record_dispatch_tick(str(board), error_class)
            return
        try:
            registry.record_dispatch_tick(str(board), error_class, locked=True)
        except TypeError:
            # A registry from before the lock mark: record the failure, as before.
            registry.record_dispatch_tick(str(board), error_class)
    except Exception:  # noqa: BLE001 - a metric must never fail the dispatcher's tick
        LOGGER.debug("dispatcher tick not recorded", exc_info=True)


def record_tick_start(board: str, tick: bool = False) -> None:
    """Record that a try on ``board`` began: a tick (``tick``), a ready probe or an auto-decompose connect. Never raises."""
    try:
        registry = chat_turn_metrics._load_registry()
        if registry is None:
            return
        if not tick:
            registry.record_dispatch_tick_start(str(board))
            return
        try:
            registry.record_dispatch_tick_start(str(board), tick=True)
        except TypeError:
            # A registry from before the tick mark: record the try, as before.
            registry.record_dispatch_tick_start(str(board))
    except Exception:  # noqa: BLE001 - a metric must never fail the dispatcher's tick
        LOGGER.debug("dispatcher tick start not recorded", exc_info=True)


def record_tick_end(board: str) -> None:
    """Record that the try on ``board`` ended, whichever way. Never raises."""
    try:
        registry = chat_turn_metrics._load_registry()
        if registry is not None:
            registry.record_dispatch_tick_end(str(board))
    except Exception:  # noqa: BLE001 - a metric must never fail the dispatcher's tick
        LOGGER.debug("dispatcher tick end not recorded", exc_info=True)
