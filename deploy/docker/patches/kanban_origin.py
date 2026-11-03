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
resolves a top-level card's origin *before* the transaction, so the Session KV
lookup never runs under the board's write lock. Every creator goes through
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
2. **The creating turn's origin** (:func:`turn_origin`): a tick-started cron
   run's context (``tools.cron_run_scope.current_cron_origin``, when that
   Hermes has it); else the session record of the turn's trusted session id
   (``kanban_priority.trusted_origin_session``), read from the Session KV
   server's ``GET /v1/sessions/{id}/metadata``; else ``console`` for a session
   id the web console (``web-console-``) or the admin console's chat page
   (``portal_``) minted; else nothing.
3. **The Hermes dashboard's create route** (``created_by="dashboard"``, which
   the route sets itself) is ``console``. Only outside the CLI create path,
   where ``--created-by`` is an argument anyone can type.
4. **A trusted writer's override, on the CLI create path only**
   (:data:`CARD_ORIGIN_ENV`). See below.

A card that resolves to none of these, ``other``, gets no stamp, and reads as
``other`` (:func:`origin_of`). That leaves room for a trusted writer's
``PUT /v1/board/tasks/{id}/origin`` on the Session KV server, which applies only
where no stamp exists. The stamp is written with ``INSERT OR IGNORE``: the first
stamp wins, and a dedupe hit (an ``idempotency_key`` that matches an existing
card) inserts nothing and stamps nothing, so the first card's stamp stands.

No argument sets the origin
---------------------------
The origin is never an argument of a create command. ``/kanban create`` serves
chat users and in-pod scripts alike, so a ``--origin`` flag would let anyone who
can chat stamp ``issue`` or ``drift``; the parser has none and rejects one. The
``kanban_create`` tool reads no such argument either.

The trusted writers' override
-----------------------------
Three in-pod writers know their card's origin better than their own context:
the GitHub watcher gate (``issue`` on issue cards, ``scheduled`` on its
pull-request follow-up cards), the drift backlog (``drift``) and the chat
bootstrap (``chat``). They run ``hermes kanban create`` (the drift backlog, a
subprocess of the Session KV server) or ``run_slash`` in their own process (the
gate and the bootstrap, under :func:`writer_origin`) with
:data:`CARD_ORIGIN_ENV` set in that process's environment to the value, then
re-stamp a card the create just inserted (:func:`created_just_now`) through the
Session KV server (:func:`put_card_origin`), which changes nothing where the
insert already stamped. Only the CLI create path reads the variable
(:func:`wrap_cli_create` marks the call), and only when steps 1 and 2 found
nothing, so a chat user's ``/kanban create`` in the gateway stamps ``chat``
whatever the environment holds. The value must be one of :data:`ORIGINS` other
than ``other``.

Only in-pod code sets that variable. The sandbox, where the agent's terminal
runs, has its own environment and no route to the board. One way remains for
the agent to put a key into a Hermes process's environment: Hermes loads
``$HERMES_HOME/.env``, which an agent can write, with ``override=True``. So an
override whose name appears in that file is refused, and the card falls back to
no stamp.

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
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

#: The origin vocabulary, as ``agents/platform/scripts/session_kv_server.py``
#: ``ORIGINS`` spells it (``test_kanban_origin`` pins the two together).
ORIGINS = ("chat", "console", "api", "event", "drift", "audit", "scheduled", "issue", "other")
ORIGIN_OTHER = "other"
ORIGIN_CONSOLE = "console"
#: What an unstamped card, or an unresolved turn, reads as.
NO_ORIGIN = (ORIGIN_OTHER, "")

#: The board side table, the schema the Session KV server's
#: ``PUT /v1/board/tasks/{id}/origin`` creates (``KANBAN_TASK_ORIGINS_SCHEMA``).
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

#: The trusted writers' override, read only on the CLI create path.
CARD_ORIGIN_ENV = "KUBE_AGENTS_CARD_ORIGIN"
#: The agent-writable dotenv file Hermes loads with ``override=True``, under
#: ``HERMES_HOME`` (default ``~/.hermes``).
HERMES_HOME_ENV = "HERMES_HOME"
DEFAULT_HERMES_HOME = "~/.hermes"
DOTENV_NAME = ".env"
_DOTENV_NAMES_OVERRIDE = re.compile(
    rf"^\s*(?:export\s+)?{re.escape(CARD_ORIGIN_ENV)}\s*=", re.MULTILINE
)

#: The Session KV server's loopback address and key, as ``kanban_notifier``
#: spells them. The server binds 127.0.0.1 only.
SESSION_KV_URL = "http://127.0.0.1:8699"
SESSION_KV_KEY_ENV = "SESSION_KV_API_KEY"
SESSION_METADATA_PATH = "/v1/sessions/{session_id}/metadata"
BOARD_ORIGIN_PATH = "/v1/board/tasks/{task_id}/origin"
#: Keys of the session record (``session_kv_server.ORIGIN_KEY`` and
#: ``ORIGIN_PLATFORM_KEY``).
ORIGIN_KEY = "origin"
ORIGIN_PLATFORM_KEY = "origin_platform"
#: A create waits at most this long for the session record. It runs before the
#: insert's transaction, on a loopback call.
SESSION_LOOKUP_TIMEOUT_SECONDS = 2.0
HTTP_NOT_FOUND = 404
#: A trusted writer's re-stamp waits at most this long; it never fails the filing.
BOARD_ORIGIN_TIMEOUT_SECONDS = 2.0
#: How old a card a create returned may be and still be one that create
#: inserted. The CLI's answer carries no "inserted" flag, and a dedupe hit
#: returns the existing card, so its ``created_at`` is the signal. It only
#: gates the re-stamp, which cannot replace a stamp anyway.
CREATED_JUST_NOW_SECONDS = 60

#: Where Hermes installs the patched modules this one reads, and the name a
#: test loads them under.
CRON_SCOPE_MODULES = ("tools.cron_run_scope", "cron_run_scope")
PRIORITY_MODULES = ("hermes_cli.kanban_priority", "kanban_priority")

#: A top-level create's origin, resolved by :func:`wrap_create_task` before the
#: insert's transaction and read inside it.
_pending_origin: contextvars.ContextVar[Optional[tuple[str, str]]] = contextvars.ContextVar(
    "kanban_origin_pending", default=None
)
#: Set while the CLI create command runs, the one path that reads the override.
_cli_create: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "kanban_origin_cli_create", default=False
)


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


def _cron_origin() -> Optional[tuple[str, str]]:
    """The tick-started cron run's origin, or None outside one (or on a Hermes without it)."""
    scope = _first_module(CRON_SCOPE_MODULES)
    current = getattr(scope, "current_cron_origin", None)
    if current is None:
        return None
    found = current()
    if not found:
        return None
    origin, platform = found
    return (origin, platform or "") if origin in ORIGINS else None


def _session_id() -> str:
    """The creating turn's trusted Hermes session id, or ``""``."""
    priority = _first_module(PRIORITY_MODULES)
    if priority is None:
        return ""
    return priority.trusted_origin_session() or ""


def _fetch_session_metadata(session_id: str) -> Optional[dict]:
    """The session's record from the Session KV server, or None when it has none."""
    token = (os.environ.get(SESSION_KV_KEY_ENV) or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = SESSION_KV_URL + SESSION_METADATA_PATH.format(
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
    return loaded if isinstance(loaded, dict) else None


def _record_origin(session_id: str) -> Optional[tuple[str, str]]:
    meta = _fetch_session_metadata(session_id)
    if not meta:
        return None
    origin = meta.get(ORIGIN_KEY)
    if not isinstance(origin, str) or origin not in ORIGINS or origin == ORIGIN_OTHER:
        return None
    platform = meta.get(ORIGIN_PLATFORM_KEY)
    return origin, platform if isinstance(platform, str) else ""


def turn_origin() -> tuple[str, str]:
    """The creating turn's ``(origin, platform)``; ``("other", "")`` when unresolved.

    The cron run's context; else the session record for the trusted session id;
    else ``console`` by the console id convention; else ``other``. Never raises.
    """
    try:
        cron = _cron_origin()
        if cron:
            return cron
        session_id = _session_id()
        if not session_id:
            return NO_ORIGIN
        record = _record_origin(session_id)
        if record:
            return record
        if session_id.startswith(CONSOLE_SESSION_PREFIXES):
            return ORIGIN_CONSOLE, ""
    except Exception as exc:  # noqa: BLE001 — never fail a create
        logger.warning("kanban origin: the turn's origin is unreadable: %r", exc)
    return NO_ORIGIN


# ---------------------------------------------------------------------------
# The trusted writers' override
# ---------------------------------------------------------------------------


def _dotenv_names_override() -> bool:
    """Whether ``$HERMES_HOME/.env`` names :data:`CARD_ORIGIN_ENV` (True when unreadable)."""
    home = Path(os.environ.get(HERMES_HOME_ENV) or DEFAULT_HERMES_HOME).expanduser()
    path = home / DOTENV_NAME
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    return bool(_DOTENV_NAMES_OVERRIDE.search(text))


def cli_override() -> Optional[tuple[str, str]]:
    """The override a trusted writer set for this CLI create, or None.

    Read only while :func:`wrap_cli_create` runs; refused when the value is not
    an origin, is ``other``, or the agent-writable dotenv file names it.
    """
    if not _cli_create.get():
        return None
    value = (os.environ.get(CARD_ORIGIN_ENV) or "").strip()
    if not value:
        return None
    if value not in ORIGINS or value == ORIGIN_OTHER:
        logger.warning("kanban origin: ignoring %s=%r, not an origin", CARD_ORIGIN_ENV, value)
        return None
    if _dotenv_names_override():
        logger.warning(
            "kanban origin: ignoring %s, named in the agent-writable %s", CARD_ORIGIN_ENV, DOTENV_NAME
        )
        return None
    return value, ""


def creation_origin(created_by: object = None) -> tuple[str, str]:
    """A top-level card's ``(origin, platform)``: the turn, then the dashboard, then the override."""
    origin = turn_origin()
    if origin != NO_ORIGIN:
        return origin
    try:
        if _cli_create.get():
            return cli_override() or NO_ORIGIN
        if created_by == DASHBOARD_CREATED_BY:
            return ORIGIN_CONSOLE, ""
    except Exception as exc:  # noqa: BLE001 — never fail a create
        logger.warning("kanban origin: the creator's origin is unreadable: %r", exc)
    return NO_ORIGIN


@contextlib.contextmanager
def writer_origin(origin: str) -> Iterator[None]:
    """Set :data:`CARD_ORIGIN_ENV` for a trusted writer's in-process ``run_slash`` create.

    Restores the variable afterwards. A writer that runs ``hermes kanban
    create`` as a subprocess sets it in the child's environment instead.
    """
    previous = os.environ.get(CARD_ORIGIN_ENV)
    os.environ[CARD_ORIGIN_ENV] = origin
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(CARD_ORIGIN_ENV, None)
        else:
            os.environ[CARD_ORIGIN_ENV] = previous


def created_just_now(created_at: object, now: Optional[float] = None) -> bool:
    """Whether a card's ``created_at`` (epoch seconds) says the create just inserted it."""
    try:
        age = (time.time() if now is None else now) - float(created_at)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= CREATED_JUST_NOW_SECONDS


def put_card_origin(task_id: str, origin: str, platform: str = "") -> bool:
    """A trusted writer's ``PUT /v1/board/tasks/{id}/origin``; True when it stamped.

    Applies only where the card has no stamp. Never raises.
    """
    token = (os.environ.get(SESSION_KV_KEY_ENV) or "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = SESSION_KV_URL + BOARD_ORIGIN_PATH.format(task_id=urllib.parse.quote(task_id, safe=""))
    body = json.dumps({"origin": origin, "platform": platform}).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers=headers, method="PUT"),
            timeout=BOARD_ORIGIN_TIMEOUT_SECONDS,
        ) as response:
            return bool(json.loads(response.read() or b"{}").get("stamped"))
    except Exception as exc:  # noqa: BLE001 — the card is filed; the stamp is telemetry
        logger.warning("kanban origin: could not re-stamp %s as %s: %r", task_id, origin, exc)
        return False


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
    """``hermes_cli.kanban._cmd_create``, marked as the path that reads the override."""

    @functools.wraps(command)
    def wrapper(*args, **kwargs):
        token = _cli_create.set(True)
        try:
            return command(*args, **kwargs)
        finally:
            _cli_create.reset(token)

    return wrapper
