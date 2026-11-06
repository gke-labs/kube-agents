"""The origin of a kanban card, stamped once at insert from trusted context.

Installed into the image at ``/opt/hermes/hermes_cli/kanban_origin.py`` and
wired into Hermes by ``deploy/docker/patches/apply_kanban_origin.py``. The
design is ``docs/designs/proposal-origin.md`` §2.1, "Cards"; the record is the
board side table ``kanban_task_origins`` rather than two columns on ``tasks``.

Why
---
A proposal is counted by the origin of the work that opened it: a person in
chat, event triage, drift, a scheduled audit, a claimed GitHub issue, a
console, an API caller. Work on the board runs as a worker of a card, so the
card has to carry its origin, and the record must come from context the model
cannot shape. ``kanban_priority`` already classifies a new card that way; this
module records the finer origin beside it.

The seam
--------
One function runs inside the insert's transaction for every card the board
creates: upstream's ``kanban_db_graph.inherit_creator_origin``. ``create_task``
calls it after its ``INSERT`` and ``_insert_decomposed_child`` after its own,
both inside ``write_txn``. The applier rebinds it to
:func:`wrap_inherit_creator_origin`, which writes the stamp in that same
transaction, so a card never exists unstamped and a rolled-back insert leaves
no stamp. ``create_task`` itself is rebound to :func:`wrap_create_task`, which
resolves a top-level card's origin *before* its own insert's transaction. A
caller that wraps several creates in one outer ``write_txn`` (``kanban swarm``:
``create_swarm`` makes N+3 cards in one transaction) still resolves under that
lock, so :func:`turn_origin` memoises the session record per session id for
:data:`ORIGIN_CACHE_TTL_SECONDS`: one Session KV lookup per turn, not one per
card. Every creator goes through
these two: the ``kanban_create`` tool, ``hermes kanban create`` and
``/kanban create`` (``run_slash``), ``kanban swarm``, the Hermes dashboard's
create route, and auto-decomposition. No kube-agents code inserts a card any
other way; the admin console files none.

What the stamp is
-----------------
In this order, the first that applies:

1. **A card with a creator inherits the creator's stamp.** A dispatcher worker's
   ``kanban_create`` and ``hermes kanban create`` pass ``HERMES_KANBAN_TASK``,
   the card the worker runs, as ``creator_task_id``; an auto-decomposed child
   passes its root. The child gets that card's stamp, or none when it has none.
2. **A trusted writer's stamp ticket** (:func:`writer_ticket`). See below.
3. **The creating turn's origin** (:func:`turn_origin`): for a dispatcher
   worker, the stamp of the card it runs (``HERMES_KANBAN_TASK``), read from
   the board on every call and never memoised, since one process can serve
   more than one card; an unstamped card falls through to the rest. Then a
   tick-started cron run's context (``tools.cron_run_scope.current_cron_origin``, when that
   Hermes has it); else the session record of the turn's trusted session id
   (``kanban_priority.trusted_origin_session``), read from the Session KV
   server's ``GET /v1/sessions/{id}/origin``, which follows a session id that
   compression rotated back to the parent that holds the record; else
   ``console`` for a session
   id the web console (``web-console-``) or the admin console's chat page
   (``portal_``) minted; else nothing.
4. **The Hermes dashboard's create route** (``created_by="dashboard"``, which
   the route sets itself) is ``console``. Only outside the CLI create and
   swarm commands, where ``--created-by`` is an argument anyone can type.

A card that resolves to none of these, ``other``, gets no stamp, and reads as
``other`` (:func:`origin_of`). The stamp is written with ``INSERT OR IGNORE``:
the first stamp wins, and a dedupe hit (an ``idempotency_key`` that matches an
existing card) inserts nothing and stamps nothing, so the first card's stamp
stands.

No argument sets the origin
---------------------------
The origin is never an argument of a create command. ``/kanban create`` serves
chat users and in-pod scripts alike, so a ``--origin`` flag would let anyone who
can chat stamp ``issue`` or ``drift``; the parser has none and rejects one. The
``kanban_create`` tool reads no such argument either.

The trusted writers' stamp ticket
---------------------------------
Three in-pod writers know their card's origin better than their own context:
the GitHub watcher gate (``issue`` on issue cards, ``scheduled`` on its
pull-request follow-up cards), the drift backlog (``drift``) and the chat
bootstrap (``chat``). None of them passes an origin value. The drift backlog
runs inside the Session KV server, so once its ``hermes kanban create``
subprocess returns the id it stamps the card in the board directly, with the
same ``INSERT OR IGNORE`` (the subprocess has no turn, so the insert left the
card unstamped). The gate (a cron script process of its own) and the bootstrap
(in the gateway's process) run ``run_slash`` in-process: just before each
create they mint a one-time ticket for their origin with
``POST /v1/board/stamp-tickets`` on the Session KV server (the pod's Session KV
key) and hand it to that one create through a ``ContextVar``
(:func:`writer_ticket`), never ``os.environ``, which is process-wide and would
let a concurrent create in the same process redeem it. Before the insert's
transaction, :func:`wrap_create_task` redeems it over loopback
(``POST /v1/board/stamp-tickets/{ticket}/redeem``, also with the pod's key) and
the insert stamps the origin the server returns, exactly and in the insert's
own transaction. A ticket redeems once and expires after the server's
``STAMP_TICKET_TTL_SECONDS``.

Why a ticket and not the origin itself: the ticket is a random reference the
server issued, not a claim. Minting a ticket that redeems needs the Session KV
key and the agent container's own
loopback, which the gateway and the in-pod scripts have and the shell-sandbox
pod, where the model's commands run (terminal, ``execute_code``, file tools),
does not: see ``docs/credential-isolation-design.md``, "The loopback-only
exception". Any failure to redeem (an unknown, spent or expired ticket, no key,
the server down) leaves the card to the turn's origin, as if there were no
ticket. A model that schedules a shipped
script through ``cronjob_manage`` (the issue gate, for example) re-runs a
trusted writer with that writer's own fixed origin, not one it chooses.

Nothing here may fail a create: every entry point logs and falls back to no
stamp.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

#: The origin vocabulary, as ``agents/platform/scripts/session_kv_server.py``
#: ``ORIGINS`` spells it (``test_kanban_origin`` pins the two together).
ORIGINS = ("chat", "console", "api", "event", "drift", "audit", "scheduled", "issue", "other")
ORIGIN_OTHER = "other"
ORIGIN_CONSOLE = "console"
#: What an unstamped card, or an unresolved turn, reads as.
NO_ORIGIN = (ORIGIN_OTHER, "")

#: The board side table, the schema the Session KV server's drift-backlog stamp
#: creates (``KANBAN_TASK_ORIGINS_SCHEMA``).
ORIGINS_TABLE = "kanban_task_origins"
_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {ORIGINS_TABLE} ("
    " task_id TEXT PRIMARY KEY,"
    " origin TEXT,"
    " origin_platform TEXT,"
    " stamped_at TEXT"
    ")"
)
_STAMP_SQL = (
    f"INSERT OR IGNORE INTO {ORIGINS_TABLE} (task_id, origin, origin_platform, stamped_at)"
    " VALUES (?, ?, ?, ?)"
)
_ORIGIN_OF_SQL = f"SELECT origin, origin_platform FROM {ORIGINS_TABLE} WHERE task_id = ?"

#: Session ids the operator-facing consoles mint for themselves: the web console
#: (``a2a/cmd/web-console`` ``sessionIDPrefix``) and the admin console's chat
#: page. Neither writes a session record, so the prefix is the convention.
CONSOLE_SESSION_PREFIXES = ("web-console-", "portal_")

#: The ``created_by`` the Hermes dashboard's create route
#: (``plugins/kanban/dashboard/plugin_api.py``) passes ``create_task``.
DASHBOARD_CREATED_BY = "dashboard"

#: The Session KV server's loopback address and key, as ``kanban_notifier``
#: spells them. The server binds 127.0.0.1 only.
SESSION_KV_URL = "http://127.0.0.1:8699"
SESSION_KV_KEY_ENV = "SESSION_KV_API_KEY"
#: The session's origin record, inherited through compression; 404 when none.
SESSION_ORIGIN_PATH = "/v1/sessions/{session_id}/origin"
STAMP_TICKETS_PATH = "/v1/board/stamp-tickets"
STAMP_TICKET_REDEEM_PATH = "/v1/board/stamp-tickets/{ticket}/redeem"
#: Keys of the origin route's answer.
ORIGIN_KEY = "origin"
ORIGIN_PLATFORM_KEY = "platform"
#: A create waits at most this long for the session record. It runs before the
#: insert's transaction, on a loopback call.
SESSION_LOOKUP_TIMEOUT_SECONDS = 2.0
HTTP_NOT_FOUND = 404
#: A session record (or its absence) is reused this long, so the creates of one
#: turn, such as a swarm's under one outer transaction, pay one lookup.
ORIGIN_CACHE_TTL_SECONDS = 30
#: Minting and redeeming a ticket each wait at most this long, on loopback.
STAMP_TICKET_TIMEOUT_SECONDS = 2.0

#: The card a dispatcher-spawned worker runs, as the dispatcher sets it.
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"

#: Where Hermes installs the patched modules this one reads, and the name a
#: test loads them under.
CRON_SCOPE_MODULES = ("tools.cron_run_scope", "cron_run_scope")
PRIORITY_MODULES = ("hermes_cli.kanban_priority", "kanban_priority")

#: A top-level create's origin, resolved by :func:`wrap_create_task` before the
#: insert's transaction and read inside it.
_pending_origin: contextvars.ContextVar[Optional[tuple[str, str]]] = contextvars.ContextVar(
    "kanban_origin_pending", default=None
)
#: An in-process writer's ticket for the create it wraps (:func:`writer_ticket`).
_writer_ticket: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "kanban_origin_writer_ticket", default=None
)
#: Set while the CLI create or swarm command runs, whose ``created_by`` is an argument.
_cli_create: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kanban_origin_cli_create", default=False
)
#: Session id -> (monotonic expiry, the record's ``(origin, platform)`` or None).
_ORIGIN_CACHE: dict[str, tuple[float, Optional[tuple[str, str]]]] = {}
_ORIGIN_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# The side table
# ---------------------------------------------------------------------------


def stamp(conn: sqlite3.Connection, task_id: str, origin: str, platform: str = "") -> bool:
    """Stamp ``task_id`` with ``origin``; True when this call wrote it.

    ``INSERT OR IGNORE``: a card that has a stamp keeps it. Raises
    ``ValueError`` for an origin outside :data:`ORIGINS`.
    """
    if origin not in ORIGINS:
        raise ValueError(f"origin {origin!r} is not one of {ORIGINS}")
    conn.execute(_TABLE_DDL)
    cursor = conn.execute(
        _STAMP_SQL, (task_id, origin, platform or "", datetime.now(timezone.utc).isoformat())
    )
    return cursor.rowcount > 0


def origin_of(conn: sqlite3.Connection, task_id: str) -> tuple[str, str]:
    """The card's ``(origin, platform)``, or ``("other", "")`` when it has no stamp."""
    try:
        row = conn.execute(_ORIGIN_OF_SQL, (task_id,)).fetchone()
    except sqlite3.OperationalError:  # a board that predates the table
        return NO_ORIGIN
    if not row or row[0] not in ORIGINS:
        return NO_ORIGIN
    return str(row[0]), str(row[1] or "")


# ---------------------------------------------------------------------------
# The creating turn
# ---------------------------------------------------------------------------


def _first_module(names: tuple[str, ...]) -> Any:
    import importlib

    for name in names:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def card_origin() -> Optional[tuple[str, str]]:
    """The stamp of the card this process runs as a dispatcher worker; None outside one or unstamped.

    Read from the board the worker already opens (``connect_closing``) on every
    call, so a process that runs a second card never reads the first one's
    stamp. Raises when the board cannot be read; :func:`turn_origin` guards it.
    """
    task_id = os.environ.get(WORKER_TASK_ENV, "").strip()
    if not task_id:
        return None
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        found = tuple(origin_of(conn, task_id))
    return None if found == NO_ORIGIN else found


def _cron_origin() -> Optional[tuple[str, str]]:
    """The tick-started cron run's origin, or None outside one (or on a Hermes without it)."""
    scope = _first_module(CRON_SCOPE_MODULES)
    current = getattr(scope, "current_cron_origin", None)
    if current is None:
        return None
    try:
        found = current()
        if not found:
            return None
        origin, platform = found
    except Exception as exc:  # noqa: BLE001 — an unreadable run context is no run
        logger.warning("kanban origin: the cron run's origin is unreadable: %r", exc)
        return None
    return (origin, platform or "") if origin in ORIGINS else None


def _session_id() -> str:
    """The creating turn's trusted Hermes session id, or ``""``."""
    priority = _first_module(PRIORITY_MODULES)
    if priority is None:
        return ""
    return priority.trusted_origin_session() or ""


def _fetch_session_origin(session_id: str) -> Optional[dict]:
    """The session's origin record from the Session KV server, or None when it has none.

    ``GET /v1/sessions/{id}/origin`` walks compression parents, so a turn whose
    id a mid-turn compression rotated still reads its root's record.
    """
    token = (os.environ.get(SESSION_KV_KEY_ENV) or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = SESSION_KV_URL + SESSION_ORIGIN_PATH.format(
        session_id=urllib.parse.quote(session_id, safe="")
    )
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=SESSION_LOOKUP_TIMEOUT_SECONDS
        ) as response:
            loaded = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code != HTTP_NOT_FOUND:
            logger.warning("kanban origin: session %s record unreadable: HTTP %s", session_id, exc.code)
        return None
    except (urllib.error.URLError, OSError, ValueError) as exc:  # down, timed out, or not JSON
        logger.warning("kanban origin: session %s record unreadable: %r", session_id, exc)
        return None
    return loaded if isinstance(loaded, dict) else None


def _record_origin(session_id: str) -> Optional[tuple[str, str]]:
    meta = _fetch_session_origin(session_id)
    if not meta:
        return None
    origin = meta.get(ORIGIN_KEY)
    if not isinstance(origin, str) or origin not in ORIGINS or origin == ORIGIN_OTHER:
        return None
    platform = meta.get(ORIGIN_PLATFORM_KEY)
    return origin, platform if isinstance(platform, str) else ""


def _cached_record_origin(session_id: str) -> Optional[tuple[str, str]]:
    """:func:`_record_origin`, reused for :data:`ORIGIN_CACHE_TTL_SECONDS`, a miss included.

    The lookup runs outside the lock, so a slow server never serialises
    unrelated sessions; two first lookups of one session may both run.
    """
    now = time.monotonic()
    with _ORIGIN_CACHE_LOCK:
        cached = _ORIGIN_CACHE.get(session_id)
        if cached is not None and cached[0] > now:
            return cached[1]
    record = _record_origin(session_id)
    expires = time.monotonic() + ORIGIN_CACHE_TTL_SECONDS
    with _ORIGIN_CACHE_LOCK:
        for stale in [key for key, (until, _) in _ORIGIN_CACHE.items() if until <= now]:
            del _ORIGIN_CACHE[stale]
        _ORIGIN_CACHE[session_id] = (expires, record)
    return record


def turn_origin() -> tuple[str, str]:
    """The creating turn's ``(origin, platform)``; ``("other", "")`` when unresolved.

    The worker's card stamp (:func:`card_origin`, never memoised); else the
    cron run's context; else the session record for the trusted session id
    (memoised per session id, :func:`_cached_record_origin`); else ``console``
    by the console id convention; else ``other``. Never raises.
    Each step is guarded on its own, so a failure in one never skips the next.
    """
    card = _step(card_origin, None, "the worker's card")
    if card:
        return card
    cron = _step(_cron_origin, None, "the cron run's origin")
    if cron:
        return cron
    session_id = _step(_session_id, "", "the turn's session id")
    if not session_id:
        return NO_ORIGIN
    record = _step(lambda: _cached_record_origin(session_id), None, "the session record")
    if record:
        return record
    if session_id.startswith(CONSOLE_SESSION_PREFIXES):
        return ORIGIN_CONSOLE, ""
    return NO_ORIGIN


def _step(read: Callable[[], Any], default: Any, what: str) -> Any:
    """``read()``, or ``default`` when it raises (logged as ``what``)."""
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 — never fail a create
        logger.warning("kanban origin: %s is unreadable: %r", what, exc)
        return default


# ---------------------------------------------------------------------------
# The trusted writers' stamp ticket
# ---------------------------------------------------------------------------


def _session_kv(path: str, payload: Optional[dict] = None) -> dict:
    """POST ``payload`` to the Session KV server with the pod's key; the JSON answer. Raises on failure."""
    token = (os.environ.get(SESSION_KV_KEY_ENV) or "").strip()
    if not token:
        raise PermissionError(f"no {SESSION_KV_KEY_ENV}")
    request = urllib.request.Request(
        SESSION_KV_URL + path,
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=STAMP_TICKET_TIMEOUT_SECONDS) as response:
        loaded = json.loads(response.read() or b"{}")
    return loaded if isinstance(loaded, dict) else {}


def mint_ticket(origin: str, platform: str = "") -> Optional[str]:
    """A one-time stamp ticket for ``origin`` from the Session KV server, or None. Never raises."""
    try:
        ticket = _session_kv(STAMP_TICKETS_PATH, {"origin": origin, "platform": platform}).get("ticket")
    except Exception as exc:  # noqa: BLE001 — the card files unstamped rather than not at all
        logger.warning("kanban origin: could not mint a stamp ticket for %s: %r", origin, exc)
        return None
    return ticket if isinstance(ticket, str) and ticket else None


@contextlib.contextmanager
def writer_ticket(origin: str, platform: str = "") -> Iterator[Optional[str]]:
    """Mint a ticket and hand it to a writer's in-process ``run_slash`` create.

    Carried in :data:`_writer_ticket`, a ``ContextVar``, and reset afterwards;
    ``os.environ`` is never touched, so a concurrent create elsewhere in the
    process cannot redeem it. Yields the ticket, or None when none was minted
    (the create then takes its own context's origin).
    """
    ticket = mint_ticket(origin, platform)
    token = _writer_ticket.set(ticket)
    try:
        yield ticket
    finally:
        _writer_ticket.reset(token)


def ticket_origin() -> Optional[tuple[str, str]]:
    """The origin this create's ticket carries, redeeming it; None otherwise.

    The ticket is :func:`writer_ticket`'s ``ContextVar``. An unknown, spent or
    expired ticket, a missing key or an unreachable server is None, as if there
    were no ticket.
    """
    ticket = (_writer_ticket.get() or "").strip()
    if not ticket:
        return None
    try:
        answer = _session_kv(
            STAMP_TICKET_REDEEM_PATH.format(ticket=urllib.parse.quote(ticket, safe=""))
        )
    except Exception as exc:  # noqa: BLE001 — a planted or stale ticket changes nothing
        logger.warning("kanban origin: ignoring the stamp ticket, it did not redeem: %r", exc)
        return None
    origin, platform = answer.get("origin"), answer.get("platform") or ""
    if origin not in ORIGINS or origin == ORIGIN_OTHER or not isinstance(platform, str):
        return None
    return origin, platform


def creation_origin(created_by: object = None) -> tuple[str, str]:
    """A top-level card's ``(origin, platform)``: a ticket, then the turn, then the dashboard."""
    try:
        ticket = ticket_origin()
        if ticket:
            return ticket
    except Exception as exc:  # noqa: BLE001 — never fail a create
        logger.warning("kanban origin: the stamp ticket is unreadable: %r", exc)
    origin = turn_origin()
    if origin != NO_ORIGIN:
        return origin
    if created_by == DASHBOARD_CREATED_BY and not _cli_create.get():
        return ORIGIN_CONSOLE, ""
    return NO_ORIGIN


# ---------------------------------------------------------------------------
# The wrappers the applier installs
# ---------------------------------------------------------------------------


def stamp_at_insert(conn: sqlite3.Connection, task_id: str, creator_task_id: object) -> None:
    """Stamp a card just inserted, inside the insert's transaction. Never raises.

    A card with a creator takes the creator's stamp; a top-level card the
    origin :func:`wrap_create_task` resolved. ``other`` writes nothing.
    """
    try:
        if creator_task_id:
            origin = origin_of(conn, str(creator_task_id))
        else:
            origin = _pending_origin.get()
            if origin is None:
                origin = creation_origin()
        if origin == NO_ORIGIN:
            return
        stamp(conn, task_id, *origin)
    except Exception as exc:  # noqa: BLE001 — never fail a create
        logger.warning("kanban origin: could not stamp %s: %r", task_id, exc)


def wrap_inherit_creator_origin(inherit: Callable[..., Any]) -> Callable[..., Any]:
    """``kanban_db_graph.inherit_creator_origin``, then the stamp, in the same transaction."""

    @functools.wraps(inherit)
    def wrapper(conn, task_id, creator_task_id, *args, **kwargs):
        result = inherit(conn, task_id, creator_task_id, *args, **kwargs)
        stamp_at_insert(conn, task_id, creator_task_id)
        return result

    return wrapper


def wrap_create_task(create_task: Callable[..., Any]) -> Callable[..., Any]:
    """``kanban_db.create_task``, with a top-level card's origin resolved before the insert."""

    @functools.wraps(create_task)
    def wrapper(conn, *args, **kwargs):
        pending = None
        if not kwargs.get("creator_task_id"):
            pending = creation_origin(kwargs.get("created_by"))
        token = _pending_origin.set(pending)
        try:
            return create_task(conn, *args, **kwargs)
        finally:
            _pending_origin.reset(token)

    return wrapper


def wrap_cli_create(command: Callable[..., Any]) -> Callable[..., Any]:
    """``hermes_cli.kanban._cmd_create`` or ``_cmd_swarm``, marked so its ``--created-by`` is not the dashboard's."""

    @functools.wraps(command)
    def wrapper(*args, **kwargs):
        token = _cli_create.set(True)
        try:
            return command(*args, **kwargs)
        finally:
            _cli_create.reset(token)

    return wrapper
