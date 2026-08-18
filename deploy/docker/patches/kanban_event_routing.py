#!/usr/bin/env python3
"""Point an event-triage card's subscription at the chat thread that raised it.

A Kubernetes event reaches the agent over the REST API. ``api_server.py``'s
session-context chokepoint binds ``platform="api_server"`` and
``chat_id=<session key>`` for that turn, and hardwires them on purpose: it is
the single structural guard against #10760, where a route that forgot to mark
its channel non-delivering reintroduced a silent no-op. Nothing about that is
wrong, and this patch does not touch it.

The consequence lands one layer down. When the front door files the triage
card, ``_maybe_auto_subscribe`` copies the ambient session values into
``kanban_notify_subs`` and writes a perfectly well-formed row addressed to
``('api_server', 'k8s-evt-<id>', '')``. The subscription exists, the notifier
finds it on every tick, and there is no such chat platform — so the card
completes, the notifier wakes, and the root-cause analysis goes nowhere. On the
install this was diagnosed against, fourteen subscription rows were sitting in
that state.

What the row is missing is not information the system lacks. The REST bridge
that opens the session recorded the originating chat route under the session id
first, in ``session_kv.db``'s ``session_metadata`` table
(``session_kv_server._register_session_routing``, called before the gateway
session exists) — the platform, the space, and the thread of the alert message
the human is looking at. The session id is
the key, and the session id is the very value sitting in the row's ``chat_id``
field. So this rewrites the three destination values at the moment the
subscription is written, and everything downstream is untouched machinery.

Confirmed on the live install before the code was written, by hand-editing one
undelivered card's row to a real chat address and rewinding its notifier
cursor: the report was delivered to the space, and with ``thread_id`` populated
it was delivered as a reply under the alert it answered. No code change was
involved in either, which is what makes the substitution sufficient rather than
merely plausible.

Two origins are treated as undeliverable. ``api_server`` is what the chokepoint
stamps; ``k8s-watcher`` is what event sessions carried in ``session_metadata``
itself before the watcher learned to record the real platform. Neither is a
chat surface, and a subscription addressed to either can never be delivered.

Everything here fails open. ``kanban_tools`` also runs inside CLI workers that
have no ``session_kv.db`` at all, so a missing file, an unreadable one, absent
metadata, malformed JSON, or a stored route that is itself non-chat all return
the caller's arguments unchanged — leaving today's behaviour exactly as it is.
A delivery-bookkeeping failure must never fail the ``kanban_create`` the agent
is mid-conversation about, which is the same contract the function this hooks
into already keeps.

Failing open on an event session does recreate the original bug, though, so
every such fall-through logs a warning naming the session and the reason. The
report is still lost, but a lost report is now a greppable line rather than a
board that reads healthy and a chat that stays empty.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from typing import Any, Optional

log = logging.getLogger(__name__)

#: Where ``session_kv_server.py`` keeps the routing table, and the environment
#: variable it reads to override the location. Kept spelled the same way here so
#: a deployment that moves the database moves both halves together.
DB_PATH_ENV = "SESSION_KV_DB_PATH"
DEFAULT_DB_PATH = "/var/lib/kube-agents/session/session_kv.db"

#: Session origins that are not chat surfaces. A subscription row addressed to
#: one of these is undeliverable by construction, which is what makes it safe to
#: look for a better address; a row naming a real chat platform is left alone.
NON_CHAT_ORIGINS = frozenset({"api_server", "k8s-watcher"})

#: How long to wait for the routing database. The caller is holding a board
#: transaction open, so this is deliberately short: losing the thread is better
#: than stalling the create.
DB_TIMEOUT_SECONDS = 2.0


def session_kv_db_path() -> str:
    """The routing database this process should read."""
    return os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH


def _stored_route(session_id: str, db_path: str) -> Optional[dict[str, Any]]:
    """The metadata the watcher recorded for ``session_id``, if any.

    Opened read-only through a URI so a reader can never create the file, take
    a write lock, or leave a stray journal next to a database another process
    owns.
    """
    if not os.path.exists(db_path):
        return None
    with closing(
        sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=DB_TIMEOUT_SECONDS
        )
    ) as conn:
        row = conn.execute(
            "SELECT metadata FROM session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    if not row or not row[0]:
        return None
    metadata = json.loads(row[0])
    return metadata if isinstance(metadata, dict) else None


def _log_undeliverable(session_id: str, reason: str) -> None:
    """Say out loud that a subscription is being written undeliverable.

    Failing open keeps ``kanban_create`` working, but it recreates the bug this
    patch exists to fix: the row is written to a non-chat origin, the card
    completes, and the report reaches nobody while the board reads healthy. The
    substitution cannot be made mandatory — a CLI worker has no routing database
    and is not doing event triage — so the only thing separating a legitimate
    fall-through from a silent drop is this line.
    """
    log.warning(
        "kanban event routing: session %s stays addressed to a non-chat origin "
        "(%s) — a report completed on this card will not reach chat",
        session_id,
        reason,
    )


def resolve_chat_route(
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    *,
    db_path: Optional[str] = None,
) -> tuple[str, str, Optional[str]]:
    """Swap a non-chat session origin for the chat route that produced it.

    Returns the arguments unchanged unless ``platform`` is a non-chat origin
    *and* ``chat_id`` keys a stored route naming a real chat platform and a
    channel. Callers can treat the result as a drop-in for what they passed in.

    When it does substitute, all three values come from the stored route,
    including ``thread_id``. The incoming thread belongs to the origin being
    replaced, so carrying it across would address the reply to a thread in a
    channel it does not live in.
    """
    if platform not in NON_CHAT_ORIGINS or not chat_id:
        return platform, chat_id, thread_id

    try:
        metadata = _stored_route(chat_id, db_path or session_kv_db_path())
    except Exception as exc:  # sqlite, JSON, permissions — all fail open
        _log_undeliverable(chat_id, f"the routing database could not be read: {exc}")
        return platform, chat_id, thread_id

    if not metadata:
        _log_undeliverable(chat_id, "no chat route was recorded for it")
        return platform, chat_id, thread_id

    route_platform = str(metadata.get("platform") or "")
    route_chat_id = str(metadata.get("chat_id") or "")
    # A stored route that is itself non-chat is the pre-fix shape ("k8s-watcher"
    # with no channel). Substituting it would swap one undeliverable address for
    # another and lose the diagnostic value of the original.
    if (
        not route_platform
        or not route_chat_id
        or route_platform in NON_CHAT_ORIGINS
    ):
        _log_undeliverable(
            chat_id,
            "the recorded route is not a chat surface either: "
            f"{route_platform or '<unset>'}/{route_chat_id or '<unset>'}",
        )
        return platform, chat_id, thread_id

    route_thread = metadata.get("thread_id") or None
    return route_platform, route_chat_id, str(route_thread) if route_thread else None
