"""Refuse to file a new card on a wake turn in a gateway conversation.

Installed into the image at ``/opt/hermes/tools/kanban_wake_guard.py`` and
wired in by ``deploy/docker/patches/apply_kanban_wake_guard.py`` at four
places: ``gateway/wake.py`` (the self-post marks its request),
``gateway/platforms/api_server.py`` (the chat-completions handler marks the
turn, and ``_run_agent`` carries the mark onto the thread the turn runs on),
and ``tools/kanban_tools.py`` (``kanban_create`` and ``kanban_unblock`` refuse on a marked turn).

Why
---
When a session files a card, upstream Hermes subscribes it to the card with
``delivery_mode="notify+wake"`` (``tools/kanban_tools.py``), and an
``api_server`` origin defaults to the same (``hermes_cli/kanban_db_notify.py``).
When the card finishes, the notifier runs a synthetic turn in the session that
filed it by POSTing to the in-pod API server (``gateway/wake.py``,
``_self_post_chat_completion``). That turn has every tool. Upstream's text for
it reads "Check the result or decide the next step ... Inspect the current
board before creating follow-up tasks", followed by the worker's result.

Under ``spec.mode: next`` the hermes-bridge drives each gateway conversation
as an ``api_server`` session named ``a2a-ctx-<context id>``. On main's
periodic next lane (issue #2610) a front-door session's first card carried the
request's limit ("Do not open a PR"), the worker answered with a proposed fix,
and the wake turn then filed a second card acting on that proposal. That
card's worker opened the pull request nobody asked for: #231 (2026-10-10) and
#210 (2026-10-11), each a few seconds after its case's card completed.

The wake turn is not a message from the person. So, as for a ``/session``
wake in the A2A gateway (#2819), it can relay or summarize what came back but
can't file a new card or unblock one: only a person's turn starts work. That's
#2205's "open a pull request only when asked" enforced at the tools rather than
left to the model reading the request. Unblock is the sibling path: a worker
that held to "don't open a PR" can block asking whether to open one, and a wake
turn that answers "yes" and unblocks it opens the PR just the same. The
blocked card's question is for the person; their reply arrives as a real turn,
which can still unblock it.

What this changes
-----------------
1. The self-post adds :data:`WAKE_HEADER` to its request. Every caller of
   ``_self_post_chat_completion`` is a turn nobody sent: the kanban notifier's
   wake, a background process's watch-pattern wake
   (``gateway/run_notifications.py``), and the Google Chat stand-in's
   (``kanban_chat_notify.py``).
2. The chat-completions handler, seeing the header, decides whether the
   session is a front-door one: its ``X-Hermes-Session-Id`` starts with
   ``a2a-ctx-``, or a session it descends from does (``parent_session_id``).
   Compression continues a long conversation under a freshly minted id, so
   the prefix alone would miss it. The walk also follows a branch or reset
   of a front-door session, which errs toward refusing. If it is, the handler sets :data:`_WAKE_TURN` for its own
   length.
3. ``_run_agent`` runs the turn on an executor thread, where ContextVars don't
   follow, so its ``run_in_executor`` call is wrapped in :func:`carry_wake_mark`,
   which sets the same value on that thread for the turn. Tool calls the agent
   runs in parallel copy that thread's context (``agent/tool_executor.py``).
4. ``kanban_create`` and ``kanban_unblock`` refuse on a marked turn. The mark belongs to the turn,
   not the session, so a person's message that lands in the same session while
   a wake turn runs is a separate turn and is untouched.

The refusal says not to file new work from this turn and to wait for the
person to ask. The wake turn can still answer, so a coordinator folding its
fan-out's results into a reply is untouched.

Scope
-----
Only front-door ``a2a-ctx-*`` sessions. The event watcher's ``k8s-evt-*``
sessions get the same wake and are a separate follow-up (#2911). Nothing else
that files cards is affected: dispatcher-spawned workers, cron, the CLI, and a
person's own turns. The guard is on ``kanban_create`` and ``kanban_unblock``.
``kanban_comment`` stays open, since a comment alone starts nothing. A wake turn
that shells out to ``hermes kanban``, or delegates, isn't covered.

Fail-open throughout: anything short of a positive "this session is on a wake
turn" lets the create through.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging

logger = logging.getLogger(__name__)

#: The header the wake self-post sends. Anyone who can reach the API server
#: with its key could send it, which only restricts their own turn.
WAKE_HEADER = "X-Kube-Agents-Wake-Turn"

#: The header upstream's API server reads the session id from.
SESSION_HEADER = "X-Hermes-Session-Id"

#: The sessions this guard covers: the hermes-bridge's per-conversation
#: sessions under ``spec.mode: next``.
GATEWAY_SESSION_PREFIX = "a2a-ctx-"

#: How far back the compression walk goes. A long thread compresses a
#: handful of times.
LINEAGE_WALK_MAX = 32

WAKE_REFUSAL = (
    "kanban_create refused: this turn is an automatic notification that a "
    "card finished, not a message from the person, so it can't start new "
    "work. Don't file a card from it. If you'd suggest a follow-up (a fix, a "
    "pull request, another investigation), keep it for when the person next "
    "writes, and wait for them to ask for it."
)

UNBLOCK_REFUSAL = (
    "kanban_unblock refused: this turn is an automatic notification that a "
    "card changed, not a message from the person, so it can't release work. "
    "If the card is waiting on a question, that question is for the person: "
    "wait for their answer, which can unblock the card."
)

#: Whether the running turn is a wake turn in a front-door session.
_WAKE_TURN: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kube_agents_kanban_wake_turn", default=False
)


def is_wake_turn() -> bool:
    return _WAKE_TURN.get()


async def _is_front_door(adapter, session_id: str) -> bool:
    """Whether ``session_id``, or a session it continues, is a front-door one.
    Walks ``parent_session_id`` through the adapter's own session store; a
    store that can't be read answers from the id alone."""
    if session_id.startswith(GATEWAY_SESSION_PREFIX):
        return True
    try:
        db = await adapter._ensure_session_db_async()
        getter = getattr(db, "get_session", None) if db is not None else None
        if not callable(getter):
            return False
        current, seen = session_id, {session_id}
        for _ in range(LINEAGE_WALK_MAX):
            row = await asyncio.to_thread(getter, current)
            parent = (row or {}).get("parent_session_id") if isinstance(row, dict) else None
            if not parent or parent in seen:
                return False
            if str(parent).startswith(GATEWAY_SESSION_PREFIX):
                return True
            seen.add(parent)
            current = parent
    except Exception as exc:  # noqa: BLE001 — fail open, but say so
        # Not debug: for a compressed front-door session this walk alone
        # decides the mark, so a store that can't be read lets the wake turn
        # file work again.
        logger.warning(
            "kanban_wake_guard: lineage of %s unreadable, so its wake turn runs "
            "unguarded: %r", session_id, exc,
        )
    return False


def wake_marked(handler):
    """Wrap the API server's chat-completions handler: a request carrying
    :data:`WAKE_HEADER` for a front-door session runs as a marked turn."""

    @functools.wraps(handler)
    async def wrapper(self, request, *args, **kwargs):
        front_door = False
        try:
            headers = request.headers
            if headers.get(WAKE_HEADER) == "1":
                session_id = (headers.get(SESSION_HEADER) or "").strip()
                if session_id:
                    front_door = await _is_front_door(self, session_id)
        except Exception as exc:  # noqa: BLE001 — never break a request
            logger.debug("kanban_wake_guard: could not read the wake marker: %r", exc)
        if not front_door:
            return await handler(self, request, *args, **kwargs)
        token = _WAKE_TURN.set(True)
        try:
            return await handler(self, request, *args, **kwargs)
        finally:
            _WAKE_TURN.reset(token)

    return wrapper


def carry_wake_mark(fn):
    """Return ``fn`` wrapped to run with this context's wake mark set. Called
    where ``_run_agent`` hands its turn to ``run_in_executor``: the mark is
    read here, on the handler's task, and set again on the executor thread."""
    marked = _WAKE_TURN.get()
    if not marked:
        return fn

    @functools.wraps(fn)
    def run():
        token = _WAKE_TURN.set(True)
        try:
            return fn()
        finally:
            _WAKE_TURN.reset(token)

    return run


def refuse_create_on_wake() -> str | None:
    """The ``kanban_create`` gate. ``None`` when the create may proceed."""
    if not is_wake_turn():
        return None
    logger.warning("kanban_wake_guard: refused kanban_create on a front-door wake turn")
    return WAKE_REFUSAL


def refuse_unblock_on_wake() -> str | None:
    """The ``kanban_unblock`` gate. ``None`` when the unblock may proceed."""
    if not is_wake_turn():
        return None
    logger.warning("kanban_wake_guard: refused kanban_unblock on a front-door wake turn")
    return UNBLOCK_REFUSAL
