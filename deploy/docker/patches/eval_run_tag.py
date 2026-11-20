"""Which eval run, case and repetition an LLM request belongs to, for the replay proxy's log.

Installed into the image at ``/opt/hermes/hermes_cli/eval_run_tag.py``. Three
callers share it:

- the API server (``apply_eval_run_tag.py`` patches
  ``gateway/platforms/api_server_openai_routes.py``): :func:`record_request`
  reads ``X-Kube-Agents-Eval-Run`` off an incoming ``/v1/chat/completions`` or
  ``/v1/responses`` request and records it once for the turn's session in the
  Session KV server (``PUT /v1/sessions/{id}/eval-run``, first write wins);
- the board (``kanban_origin.py`` calls :func:`resolve_new_card` before a
  top-level card's insert and :func:`stamp_new_card` inside it): a card takes
  its creator card's tag, else the tag of the card the creating worker runs,
  else the creating turn's session record, in the side table
  ``kanban_task_eval_runs``;
- the ``eval_run_tag`` plugin (``agents/platform/plugins/eval_run_tag``):
  :func:`on_llm_request` adds the header to every model request Hermes' turn
  loop makes, from the card the worker runs (``HERMES_KANBAN_TASK``) or the
  turn's session record, and :data:`AGENT_HEADER` naming the profile and where
  the tag came from.

The replay proxy logs both headers on every request line and never forwards
them (``hack/replay-proxy/replay_proxy.py`` ``DROPPED_REQUEST_HEADERS``). They
are HTTP headers only: nothing here touches a message, a tool or any other
part of the body the model reads or the proxy keys on.

Off unless ``KUBEAGENTS_EVAL_RUN_TAGS`` is ``1`` (the chart's
``platformAgent.deployment.evalRunTags``, which only the eval deploy turns on).
Off, every entry point returns at once: the API server ignores the header, the
board writes nothing, and the plugin registers no middleware. On, a failure
anywhere is logged and the request, card or model call goes ahead untagged.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The switch, and the values that turn it on.
ENABLE_ENV = "KUBEAGENTS_EVAL_RUN_TAGS"
ENABLED_VALUES = ("1", "true", "yes", "on")

#: ``<run>/<case>/<rep>``: the harness's run id, the case id and the repetition.
HEADER = "X-Kube-Agents-Eval-Run"
#: ``<profile>;<source>``: which Hermes profile sent the request and where its
#: tag came from (:data:`SOURCE_CARD`, :data:`SOURCE_SESSION`, :data:`SOURCE_NONE`),
#: so an untagged line still says which agent made it.
AGENT_HEADER = "X-Kube-Agents-Eval-Agent"
SOURCE_CARD = "card"
SOURCE_SESSION = "session"
SOURCE_NONE = "none"
AGENT_SEPARATOR = ";"
#: A tag: three ``/``-separated segments, the last a repetition number. The
#: replay proxy and the Session KV server hold the same pattern.
TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[0-9]{1,6}")
PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DEFAULT_PROFILE = "default"
PROFILES_DIR = "profiles"
HERMES_HOME_ENV = "HERMES_HOME"

#: The Session KV server, as ``kanban_origin`` spells it.
SESSION_KV_URL = "http://127.0.0.1:8699"
SESSION_KV_KEY_ENV = "SESSION_KV_API_KEY"
SESSION_EVAL_RUN_PATH = "/v1/sessions/{session_id}/eval-run"
EVAL_RUN_KEY = "eval_run"
HTTP_NOT_FOUND = 404
SESSION_KV_TIMEOUT_SECONDS = 2.0
#: A session with no record is asked again after this long; a found tag never
#: changes (first write wins), so it is kept for the life of the process.
MISS_CACHE_TTL_SECONDS = 30.0
#: Entries kept per cache before the oldest are dropped.
CACHE_MAX_ENTRIES = 1024

#: The card a dispatcher-spawned worker runs, as the dispatcher sets it.
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"

#: The board side table.
TABLE = "kanban_task_eval_runs"
_TABLE_DDL = f"CREATE TABLE IF NOT EXISTS {TABLE} (task_id TEXT PRIMARY KEY, eval_run TEXT, stamped_at TEXT)"
_STAMP_SQL = f"INSERT OR IGNORE INTO {TABLE} (task_id, eval_run, stamped_at) VALUES (?, ?, ?)"
_TAG_OF_SQL = f"SELECT eval_run FROM {TABLE} WHERE task_id = ?"

#: Where Hermes installs the module that names the creating turn's session.
PRIORITY_MODULES = ("hermes_cli.kanban_priority", "kanban_priority")

#: A top-level card's tag, resolved before its insert's transaction.
_pending: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("eval_run_tag_pending", default=None)

_SESSION_CACHE: dict[str, tuple[float, str]] = {}
_CARD_CACHE: dict[str, str] = {}
_CACHE_LOCK = threading.Lock()


def enabled() -> bool:
    """True when the install turned tagging on."""
    return (os.environ.get(ENABLE_ENV) or "").strip().lower() in ENABLED_VALUES


def valid_tag(value: object) -> str:
    """``value`` when it is a well-formed tag, else ``""``."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if TAG_RE.fullmatch(value) else ""


# ---------------------------------------------------------------------------
# The session record (Session KV)
# ---------------------------------------------------------------------------


def _session_kv(method: str, session_id: str, payload: Optional[dict] = None) -> dict:
    """One call to the session's eval-run route; the JSON answer. Raises on failure."""
    token = (os.environ.get(SESSION_KV_KEY_ENV) or "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = SESSION_KV_URL + SESSION_EVAL_RUN_PATH.format(session_id=urllib.parse.quote(session_id, safe=""))
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=SESSION_KV_TIMEOUT_SECONDS) as response:
        loaded = json.loads(response.read() or b"{}")
    return loaded if isinstance(loaded, dict) else {}


def record_session_tag(session_id: str, tag: str) -> bool:
    """Record ``tag`` for ``session_id`` unless it has one; True when the call succeeded. Never raises."""
    tag = valid_tag(tag)
    if not tag or not session_id:
        return False
    try:
        _session_kv("PUT", session_id, {EVAL_RUN_KEY: tag})
    except Exception as exc:  # noqa: BLE001 — the turn runs untagged rather than not at all
        logger.warning("eval run tag: could not record %s for session %s: %r", tag, session_id, exc)
        return False
    return True


def _fetch_session_tag(session_id: str) -> str:
    """The session's recorded tag, inherited through compression; ``""`` when none or unreadable."""
    try:
        answer = _session_kv("GET", session_id)
    except urllib.error.HTTPError as exc:
        if exc.code != HTTP_NOT_FOUND:
            logger.warning("eval run tag: session %s record unreadable: HTTP %s", session_id, exc.code)
        return ""
    except Exception as exc:  # noqa: BLE001 — down, timed out, or not JSON
        logger.warning("eval run tag: session %s record unreadable: %r", session_id, exc)
        return ""
    return valid_tag(answer.get(EVAL_RUN_KEY))


def _trim(cache: dict) -> None:
    while len(cache) > CACHE_MAX_ENTRIES:
        cache.pop(next(iter(cache)))


def session_tag(session_id: str) -> str:
    """:func:`_fetch_session_tag`, a found tag kept, a miss kept :data:`MISS_CACHE_TTL_SECONDS`. Never raises."""
    if not session_id:
        return ""
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _SESSION_CACHE.get(session_id)
        if cached is not None and (cached[1] or cached[0] > now):
            return cached[1]
    tag = _fetch_session_tag(session_id)
    with _CACHE_LOCK:
        _SESSION_CACHE[session_id] = (time.monotonic() + MISS_CACHE_TTL_SECONDS, tag)
        _trim(_SESSION_CACHE)
    return tag


async def record_request(request: Any, session_id: str) -> None:
    """The API server's hook: record the request's tag for its session. Never raises.

    Returns at once, the header unread, while tagging is off. Awaited before
    the turn starts, so the turn's first model call finds the record.
    """
    if not enabled():
        return
    try:
        tag = valid_tag(request.headers.get(HEADER, ""))
        if tag and session_id:
            await asyncio.to_thread(record_session_tag, str(session_id), tag)
    except Exception as exc:  # noqa: BLE001 — never fail a turn over a tag
        logger.warning("eval run tag: the request's tag was not recorded: %r", exc)


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------


def stamp_card(conn: sqlite3.Connection, task_id: str, tag: str) -> bool:
    """Stamp ``task_id`` with ``tag`` (first stamp wins); True when this call wrote it."""
    tag = valid_tag(tag)
    if not tag:
        return False
    conn.execute(_TABLE_DDL)
    cursor = conn.execute(_STAMP_SQL, (task_id, tag, datetime.now(timezone.utc).isoformat()))
    return cursor.rowcount > 0


def card_tag_in(conn: sqlite3.Connection, task_id: str) -> str:
    """The card's tag, or ``""`` (a board without the table included)."""
    try:
        row = conn.execute(_TAG_OF_SQL, (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    return valid_tag(row[0]) if row else ""


def worker_card_tag() -> str:
    """The tag of the card this process runs as a dispatcher worker; ``""`` outside one.

    A found tag is kept per card (a stamp never changes); a miss is read again
    next time. Raises when the board cannot be read.
    """
    task_id = (os.environ.get(WORKER_TASK_ENV) or "").strip()
    if not task_id:
        return ""
    with _CACHE_LOCK:
        cached = _CARD_CACHE.get(task_id)
    if cached:
        return cached
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        tag = card_tag_in(conn, task_id)
    if tag:
        with _CACHE_LOCK:
            _CARD_CACHE[task_id] = tag
            _trim(_CARD_CACHE)
    return tag


def _first_module(names: tuple[str, ...]) -> Any:
    import importlib

    for name in names:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    return None


def _turn_session_id() -> str:
    """The creating turn's trusted session id, or ``""``."""
    priority = _first_module(PRIORITY_MODULES)
    if priority is None:
        return ""
    return priority.trusted_origin_session() or ""


def _guarded(read: Any, what: str) -> str:
    try:
        return read() or ""
    except Exception as exc:  # noqa: BLE001 — never fail a create or a model call
        logger.warning("eval run tag: %s is unreadable: %r", what, exc)
        return ""


def creating_turn_tag() -> str:
    """A top-level card's tag: the worker's card, else the turn's session record. Never raises."""
    return _guarded(worker_card_tag, "the worker's card") or _guarded(
        lambda: session_tag(_turn_session_id()), "the session record"
    )


def resolve_new_card(creator_task_id: object) -> Any:
    """Before a create's transaction: resolve a top-level card's tag; a reset token, or None.

    None, and nothing done, while tagging is off or the card has a creator
    (whose stamp :func:`stamp_new_card` reads inside the transaction).
    """
    if not enabled() or creator_task_id:
        return None
    try:
        return _pending.set(creating_turn_tag() or None)
    except Exception as exc:  # noqa: BLE001 — never fail a create
        logger.warning("eval run tag: a new card's tag was not resolved: %r", exc)
        return None


def reset_new_card(token: Any) -> None:
    """Undo :func:`resolve_new_card`. Never raises."""
    if token is None:
        return
    try:
        _pending.reset(token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("eval run tag: could not reset the pending tag: %r", exc)


def stamp_new_card(conn: sqlite3.Connection, task_id: str, creator_task_id: object) -> None:
    """Inside a card's insert: stamp it with its creator's tag or the resolved one. Never raises."""
    if not enabled():
        return
    try:
        if creator_task_id:
            tag = card_tag_in(conn, str(creator_task_id))
        else:
            tag = _pending.get() or ""
        if tag:
            stamp_card(conn, task_id, tag)
    except Exception as exc:  # noqa: BLE001 — never fail a create
        logger.warning("eval run tag: could not stamp %s: %r", task_id, exc)


# ---------------------------------------------------------------------------
# The model request
# ---------------------------------------------------------------------------


def profile_name() -> str:
    """The Hermes profile this process runs, from ``HERMES_HOME``: ``.../profiles/<name>`` or ``default``."""
    home = Path(os.environ.get(HERMES_HOME_ENV) or "")
    name = home.name if home.parent.name == PROFILES_DIR else DEFAULT_PROFILE
    return name if PROFILE_RE.fullmatch(name) else DEFAULT_PROFILE


def current_tag(session_id: str = "") -> tuple[str, str]:
    """``(tag, source)`` for this process's model request now. Never raises.

    The worker's card first, read per call through the per-card cache since
    one process can serve more than one card; else the turn's session record.
    """
    tag = _guarded(worker_card_tag, "the worker's card")
    if tag:
        return tag, SOURCE_CARD
    tag = _guarded(lambda: session_tag(str(session_id or "")), "the session record")
    if tag:
        return tag, SOURCE_SESSION
    return "", SOURCE_NONE


def on_llm_request(request: Any = None, session_id: str = "", **_: Any) -> Optional[dict]:
    """``llm_request`` middleware: the request with the two headers added, or None to leave it.

    Copies ``extra_headers`` rather than editing it, as upstream's own
    ``_set_extra_header`` does. Never raises.
    """
    if not enabled() or not isinstance(request, dict):
        return None
    try:
        tag, source = current_tag(session_id)
        headers = dict(request.get("extra_headers") or {})
        headers[AGENT_HEADER] = f"{profile_name()}{AGENT_SEPARATOR}{source}"
        if tag:
            headers[HEADER] = tag
        return {"request": {**request, "extra_headers": headers}, "source": "eval_run_tag"}
    except Exception as exc:  # noqa: BLE001 — the call goes untagged
        logger.warning("eval run tag: the model request was not tagged: %r", exc)
        return None
