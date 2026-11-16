"""Stop the API-server turn running in a session, by the session's id.

Installed into the image at ``/opt/hermes/tools/api_session_stop.py`` and
wired in by ``deploy/docker/patches/apply_api_session_stop.py`` at six places:
``gateway/platforms/api_server.py`` (the chat-completions handler records the
turn under its ``X-Hermes-Session-Id``, ``_run_agent`` carries the record onto
the thread the turn runs on, the turn's agent is bound to it once created, and
the route table gains :data:`ROUTE_PATH`) and ``tools/kanban_tools.py``
(``kanban_create`` refuses on a stopped turn).

Why
---
Under ``spec.mode: next`` the hermes-bridge runs each gateway conversation as
turns in the API-server session ``a2a-<contextId>``, POSTed to
``/v1/chat/completions`` without streaming. A Stop that lands while a turn runs
cancels the bridge's request, but upstream interrupts the agent only when a
streaming client disconnects, so the turn ran on and could still file a card.
On 2026-10-26 a Stop pressed half a second into a turn ended the bridge's task
and ran the card stop, which found no card; 18 s later the cancelled turn filed
one, and it ran to completion (gke-labs/kube-agents#3263). Upstream's own stop,
``POST /v1/runs/{run_id}/stop``, needs a run id the chat-completions route
never hands out.

What this changes
-----------------
1. :func:`track` wraps the chat-completions handler: a request naming a session
   is recorded as one of that session's running turns until the handler
   returns and the turn's thread has ended.
2. :func:`carry` wraps the function ``_run_agent`` hands to
   ``run_in_executor``, so the record reaches the turn's thread (ContextVars
   don't follow an executor), and holds it until that thread ends.
3. :func:`bind` records the turn's agent once ``_run_agent`` has created it,
   and interrupts it at once if the session was stopped before it existed.
4. :data:`ROUTE_PATH`, authenticated with the server's key: every turn running
   in the session is marked stopped and its agent gets upstream's hard
   interrupt and process reap, the pair ``/v1/runs/{run_id}/stop`` uses. The
   answer waits, up to :data:`WAIT_SECONDS`, for those turns to end.
5. :func:`refuse_create_on_stop`, at ``kanban_create``: a stopped turn can't
   file a card, for a tool call already past the interrupt's checks.

The mark belongs to the turns running at the stop, not the session, so the next
message in the conversation runs normally. A request with no session header is
untouched.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import threading
import time
import types

logger = logging.getLogger(__name__)

#: The header upstream's API server reads the session id from.
SESSION_HEADER = "X-Hermes-Session-Id"

#: The stop route. The ``kube-agents`` segment keeps it clear of upstream's own
#: session routes; the route table also mirrors it under ``/p/<profile>``.
ROUTE_PATH = "/v1/kube-agents/sessions/{session_id}/stop"

#: The longest session id the route accepts, the API server's own header cap.
MAX_SESSION_ID_LEN = 256

#: How long the route waits for the stopped turns to end before it answers.
#: The bridge holds the session's turn across this wait and its card stop waits
#: for that turn (``a2a/hermes-bridge/turnstop.go``), so raise them together.
WAIT_SECONDS = 10.0

#: How often the route looks for the stopped turns to have ended.
POLL_SECONDS = 0.1

#: The interrupt's message and the process reap's source, for Hermes's logs.
INTERRUPT_MESSAGE = "Stop requested from chat"
REAP_SOURCE = "api_server_session_stop"

CREATE_REFUSAL = (
    "kanban_create refused: the person stopped this conversation, so this turn "
    "can't start new work. Don't file a card; end the turn."
)

_LOCK = threading.Lock()

#: The running turn's record, on the handler's task and the turn's thread.
_CURRENT: contextvars.ContextVar[_Turn | None] = contextvars.ContextVar(
    "kube_agents_api_session_turn", default=None
)


class _Turn:
    """One chat-completions request naming a session. ``holds`` counts the
    handler and the turn's thread; the turn is running while either does."""

    __slots__ = ("agent", "holds", "session_id", "stopped")

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.stopped = False
        self.agent = None
        self.holds = 1


def _turns(adapter) -> dict:
    """The adapter's running turns by session id. Call under ``_LOCK``."""
    turns = getattr(adapter, "_kube_session_turns", None)
    if turns is None:
        turns = adapter._kube_session_turns = {}
    return turns


def _hold(adapter, turn: _Turn) -> None:
    with _LOCK:
        turn.holds += 1
        _turns(adapter).setdefault(turn.session_id, set()).add(turn)


def _release(adapter, turn: _Turn) -> None:
    with _LOCK:
        turn.holds -= 1
        if turn.holds > 0:
            return
        turns = _turns(adapter)
        running = turns.get(turn.session_id)
        if running is not None:
            running.discard(turn)
            if not running:
                del turns[turn.session_id]


def _hard_interrupt(agent) -> None:
    """Upstream's explicit stop, then the reap of the background processes the
    turn started: what ``/v1/runs/{run_id}/stop`` does to its run's agent."""
    from agent.interrupt_compat import request_hard_interrupt
    from gateway.platforms.api_server import _reap_disconnected_agent_processes

    try:
        request_hard_interrupt(agent, INTERRUPT_MESSAGE)
    except Exception as exc:  # noqa: BLE001 — the reap still runs
        logger.warning("api_session_stop: interrupt failed: %r", exc)
    _reap_disconnected_agent_processes(agent, source=REAP_SOURCE)


def track(handler):
    """Wrap the chat-completions handler: a request naming a session is one of
    that session's running turns until it returns."""

    @functools.wraps(handler)
    async def wrapper(self, request, *args, **kwargs):
        session_id = (request.headers.get(SESSION_HEADER) or "").strip()
        if not session_id or len(session_id) > MAX_SESSION_ID_LEN:
            return await handler(self, request, *args, **kwargs)
        turn = _Turn(session_id)
        with _LOCK:
            _turns(self).setdefault(session_id, set()).add(turn)
        token = _CURRENT.set(turn)
        try:
            return await handler(self, request, *args, **kwargs)
        finally:
            _CURRENT.reset(token)
            _release(self, turn)

    return wrapper


def carry(adapter, fn):
    """Return ``fn`` wrapped to run with this context's turn record, holding
    the record until ``fn`` returns. Called where ``_run_agent`` hands its turn
    to ``run_in_executor``, on the handler's task, so the hold is taken before
    the handler can release."""
    turn = _CURRENT.get()
    if turn is None:
        return fn
    _hold(adapter, turn)

    @functools.wraps(fn)
    def run():
        token = _CURRENT.set(turn)
        try:
            return fn()
        finally:
            _CURRENT.reset(token)
            _release(adapter, turn)

    return run


def bind(agent) -> None:
    """Record the running turn's agent. A session stopped before the agent
    existed interrupts it now; the interrupt survives into the turn, since the
    turn's start keeps a pending interrupt (``_bind_interrupt_scope``)."""
    turn = _CURRENT.get()
    if turn is None:
        return
    with _LOCK:
        turn.agent = agent
        stopped = turn.stopped
    if stopped:
        logger.info("api_session_stop: session %s was stopped before its turn started; interrupting it", turn.session_id)
        _hard_interrupt(agent)


def stop(adapter, session_id: str) -> tuple[list, int]:
    """Mark every turn running in ``session_id`` stopped and interrupt each one
    whose agent exists. Returns the turns and how many were interrupted."""
    with _LOCK:
        turns = list(_turns(adapter).get(session_id, ()))
        for turn in turns:
            turn.stopped = True
        agents = [turn.agent for turn in turns if turn.agent is not None]
    for agent in agents:
        _hard_interrupt(agent)
    return turns, len(agents)


def _running(turns: list) -> int:
    with _LOCK:
        return sum(1 for turn in turns if turn.holds > 0)


async def handle(adapter, request):
    """``POST`` :data:`ROUTE_PATH`: stop the session's running turns and answer,
    once they have ended or :data:`WAIT_SECONDS` has passed, with how many
    there were, how many had an agent to interrupt, and whether all ended."""
    from aiohttp import web

    auth_err = adapter._check_auth(request)
    if auth_err:
        return auth_err
    session_id = (request.match_info.get("session_id") or "").strip()
    if not session_id or len(session_id) > MAX_SESSION_ID_LEN:
        return web.json_response({"error": "invalid session id"}, status=400)
    turns, interrupted = stop(adapter, session_id)
    deadline = time.monotonic() + WAIT_SECONDS
    while _running(turns) and time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
    running = _running(turns)
    if turns:
        logger.info(
            "api_session_stop: session %s: %d turn(s) stopped, %d interrupted, %d still running",
            session_id, len(turns), interrupted, running,
        )
    return web.json_response({
        "session_id": session_id,
        "turns": len(turns),
        "interrupted": interrupted,
        "ended": running == 0,
    })


def route(adapter) -> tuple:
    """The route-table row for :data:`ROUTE_PATH`, bound to ``adapter``."""
    return ("POST", ROUTE_PATH, types.MethodType(handle, adapter))


def refuse_create_on_stop() -> str | None:
    """The ``kanban_create`` gate. ``None`` when the create may proceed."""
    turn = _CURRENT.get()
    if turn is None or not turn.stopped:
        return None
    logger.warning("api_session_stop: refused kanban_create on a stopped turn in %s", turn.session_id)
    return CREATE_REFUSAL
