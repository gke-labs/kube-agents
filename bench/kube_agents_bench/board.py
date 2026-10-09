"""Read delegated cards straight off the kanban board, without a model turn.

The delegation wait in :mod:`kube_agents_bench.harness` used to learn whether a
card had settled by re-prompting the front door every poll interval to call
``kanban_show``. Each of those status turns replays the whole conversation, so
a card that ran for 45 minutes cost about ninety model turns and several
million input tokens before the harness even had a result to grade -- and
those tokens came out of the same per-minute quota the worker under test was
trying to use.

The board itself is a SQLite file on the agent's data volume, the same one
:mod:`kube_agents_bench.worker_trajectory` reads the workers' cards from once
they settle. This module has two reads, each one ``kubectl exec`` of the kind
the harness already relies on for artifacts and session stores:

- :func:`read_statuses`, the ``status`` column of the awaited cards. The api
  transport's wait asks the board each poll and spends a front-door turn only
  when the board says a card has stopped moving, which is the one turn that
  can carry the card's result back into the conversation.
- :func:`read_session_cards`, for the inject transport under the bridge's
  ``api`` executor, where no turn can carry a result back. It finds the cards
  one Hermes session filed, from that session's ``kanban_create`` results in
  the session store or the board's subscriptions addressed to it, and reads
  each card's status and deliverable (``tasks.result``, the newest
  ``task_runs.summary``). The wait polls it alone and sends no turn.

Best effort in the same sense as the artifact read-back: a pod that cannot
be reached or a store that cannot be opened returns ``None`` for that read,
and the caller decides what to do without it -- a status turn on the api
transport, a retry and then a fallback on the inject one. A wrong reading is
worse than no reading, so each in-pod script prints a sentinel before its JSON
and a reply without it is a failed read, never an empty one.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kube_agents_bench.parsing import _TASK_ID_RE, DELEGATION_TOOL, delegated_task_ids
from kube_agents_bench.worker_trajectory import DATA_ROOT, FALLBACK_PYTHON, HERMES_PYTHON

__all__ = ["SessionCards", "api_session_id", "read_session_cards", "read_statuses"]

_log = logging.getLogger("kube_agents_bench.board")

# Line the in-pod script prints before its JSON. A reply without it means the
# script never ran to completion.
BOARD_PRESENT = "__KANBAN_BOARD__"

# DATA_ROOT, the hermes data volume, and the two interpreters are imported
# from worker_trajectory (which documents the volume's layout) so a base-image
# change moves both readers at once. ``kanban.db`` sits at the volume's root;
# DATA_ROOT is passed to the script as an argument so the tests can point it
# at a temporary tree.

# The board file under DATA_ROOT. Named here rather than in the script so the
# harness side and the tests spell it once.
BOARD_FILE = "kanban.db"

# Runs inside the agent container. Plain ``sqlite3`` on a read-only URI, so a
# writer holding the WAL lock is waited on briefly rather than fought with,
# and nothing from hermes is imported. Positional arguments carry the data
# root, the board file, the sentinel and then the card ids.
_IN_POD_SCRIPT = r"""
import json, sqlite3, sys

ROOT, BOARD, SENTINEL = sys.argv[1:4]
ids = [a for a in sys.argv[4:] if a]
# Seconds a read waits on a locked store before giving up. A hermes writer
# holds a WAL lock for milliseconds; anything longer is stuck.
SQLITE_BUSY_TIMEOUT = 10
out = {"statuses": {}, "error": None}
try:
    conn = sqlite3.connect("file:%s/%s?mode=ro" % (ROOT, BOARD), uri=True, timeout=SQLITE_BUSY_TIMEOUT)
    marks = ",".join("?" for _ in ids)
    rows = conn.execute("SELECT id, status FROM tasks WHERE id IN (%s)" % marks, ids).fetchall()
    conn.close()
    for tid, status in rows:
        out["statuses"][str(tid)] = str(status)
except sqlite3.Error as exc:
    out["error"] = "kanban board: %s" % exc
print(SENTINEL)
print(json.dumps(out))
"""


# --- the cards one Hermes session filed ---------------------------------------
#
# Under the hermes bridge's ``api`` executor an inject conversation's turns run
# in one Hermes session of the platform agent's default profile, named after the
# conversation's A2A contextId (``apiSessionID``, a2a/hermes-bridge/api.go).
# The inject door carries no card id -- the activity trace reduces every string
# to its shape and carries no result -- so the delegation wait finds the cards
# here instead: the ``kanban_create`` tool results that session stored, else the
# board's notify subscriptions addressed to it. Then it reads each card's status
# and deliverable off the board, the same columns ``kanban_show`` returns.

# Mirrors of apiSessionIDPrefix, apiContextIDMaxLen and apiSafeContextID in
# a2a/hermes-bridge/api.go: the bridge uses a contextId verbatim under the
# prefix when it is path-safe and no longer than the cap, and hashes it
# otherwise. Only the verbatim half is mirrored. The gateway mints every
# contextId as ``ctx-<hex>`` (gateway.go, the session record), which is always
# verbatim, so an id the bridge would hash is not one this wait can be handed.
API_SESSION_PREFIX = "a2a-"
API_CONTEXT_ID_MAX_LEN = 128
_API_SAFE_CONTEXT_ID = re.compile(r"\A[A-Za-z0-9_-]+\Z")

# The default profile's session store under DATA_ROOT, the store the API server
# writes (worker_trajectory.state_db("default")).
STORE_FILE = "state.db"

# Line the session-cards script prints before its JSON.
SESSION_CARDS_PRESENT = "__KANBAN_SESSION_CARDS__"

# Bounds applied inside the pod. A turn files a handful of cards; the caps are
# runaway guards that keep the exec output bounded. A create result is a small
# JSON object; the clip only stops a pathological one carrying the record away.
MAX_SESSION_CARDS = 64
MAX_CREATE_RESULT_CHARS = 2000
# How many compression continuations the session read follows, the defensive
# bound the pinned hermes' own get_compression_chain uses
# (hermes_state_compression.py); a chain this deep is pathological.
MAX_CHAIN_STEPS = 100


# Runs inside the agent container, read-only like the status read. Positional
# arguments: data root, board file, store file, sentinel, session id, the
# create tool's name, the card cap, the create-result clip, the chain bound.
_SESSION_CARDS_SCRIPT = r"""
import json, sqlite3, sys

ROOT, BOARD, STORE, SENTINEL, SID, CREATE = sys.argv[1:7]
MAX_CARDS, MAX_CHARS, MAX_CHAIN_STEPS = (int(a) for a in sys.argv[7:10])
SQLITE_BUSY_TIMEOUT = 10
JSON_PREFIX = "\x00json:"
out = {"session": False, "sessions": [], "created": [], "subscribed": [], "cards": {},
       "error": None}


def ro(name):
    return sqlite3.connect("file:%s/%s?mode=ro" % (ROOT, name), uri=True,
                           timeout=SQLITE_BUSY_TIMEOUT)


def has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def columns(conn, table):
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def text(content):
    if isinstance(content, str) and content.startswith(JSON_PREFIX):
        try:
            content = json.loads(content[len(JSON_PREFIX):])
        except ValueError:
            pass
    if not isinstance(content, str):
        content = json.dumps(content)
    return content[:MAX_CHARS]


def marks(values):
    return ",".join("?" for _ in values)


try:
    store = ro(STORE)
    sessions = [SID]
    if has_table(store, "sessions"):
        if store.execute("SELECT 1 FROM sessions WHERE id = ?", (SID,)).fetchone():
            out["session"] = True
        # A compressed session continues under a child id. Follow that chain
        # and nothing else, one step at a time, by the pinned hermes' own rule
        # (_CHAIN_STEP_SQL, hermes_state_compression.py): the parent ended in
        # compression, and the child is not a /branch, delegate or tool child.
        # Its ordering is kept less the last-activity term. A store too old
        # for a column skips the condition that reads it.
        cols = columns(store, "sessions")
        if {"parent_session_id", "end_reason"} <= cols:
            where = ["parent.id = ?", "parent.end_reason = 'compression'"]
            if "model_config" in cols:
                for marker in ("$._branched_from", "$._delegate_from"):
                    where.append(
                        "json_extract(CASE WHEN json_valid(child.model_config) "
                        "THEN child.model_config ELSE json_object() END, '%s') IS NULL" % marker)
            if "source" in cols:
                where.append("COALESCE(child.source, '') != 'tool'")
            order = ["CASE WHEN child.end_reason = 'compression' THEN 0 "
                     + ("WHEN child.ended_at IS NULL THEN 1 " if "ended_at" in cols else "")
                     + "ELSE 2 END"]
            if "started_at" in cols:
                order.append("child.started_at DESC")
            order.append("child.id DESC")
            step = ("SELECT child.id FROM sessions parent "
                    "JOIN sessions child ON child.parent_session_id = parent.id "
                    "WHERE %s ORDER BY %s LIMIT 1" % (" AND ".join(where), ", ".join(order)))
            current = SID
            for _ in range(MAX_CHAIN_STEPS):
                row = store.execute(step, (current,)).fetchone()
                if row is None or row[0] in sessions:
                    break
                current = row[0]
                sessions.append(current)
    rows = store.execute(
        "SELECT role, content, tool_calls, tool_call_id, tool_name FROM messages "
        "WHERE session_id IN (%s) ORDER BY id" % marks(sessions), sessions).fetchall()
    store.close()
    if rows:
        out["session"] = True
    out["sessions"] = sessions
    calls = set()
    for role, content, tool_calls, call_id, tool_name in rows:
        if role == "assistant" and tool_calls:
            try:
                parsed = json.loads(tool_calls) if isinstance(tool_calls, str) else tool_calls
            except ValueError:
                parsed = []
            for tc in parsed if isinstance(parsed, list) else []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                if fn.get("name") == CREATE and (tc.get("id") or tc.get("call_id")):
                    calls.add(tc.get("id") or tc.get("call_id"))
        elif role == "tool" and (tool_name == CREATE or (call_id and call_id in calls)):
            if len(out["created"]) < MAX_CARDS:
                out["created"].append(text(content))
except sqlite3.Error as exc:
    out["error"] = "session store: %s" % exc

if out["error"] is None:
    try:
        board = ro(BOARD)
        if has_table(board, "kanban_notify_subs"):
            query = ("SELECT DISTINCT task_id FROM kanban_notify_subs WHERE chat_id IN (%s)"
                     % marks(out["sessions"]))
            # A worker's child card inherits its creator's subscriptions; the
            # wait follows only the cards this session filed, as it does on
            # every other transport.
            if has_table(board, "kanban_worker_children"):
                query += " AND task_id NOT IN (SELECT child_id FROM kanban_worker_children)"
            query += " ORDER BY rowid LIMIT %d" % MAX_CARDS
            out["subscribed"] = [str(r[0]) for r in board.execute(query, out["sessions"])]
        ids = list(out["subscribed"])
        for created in out["created"]:
            try:
                tid = json.loads(created).get("task_id")
            except (ValueError, AttributeError):
                tid = None
            if isinstance(tid, str) and tid and tid not in ids:
                ids.append(tid)
        ids = ids[:MAX_CARDS]
        if ids:
            result_col = "result" if "result" in columns(board, "tasks") else "NULL"
            for tid, status, result in board.execute(
                "SELECT id, status, %s FROM tasks WHERE id IN (%s)" % (result_col, marks(ids)), ids
            ):
                out["cards"][str(tid)] = {"status": str(status), "result": result, "summary": None}
            if has_table(board, "task_runs") and "summary" in columns(board, "task_runs"):
                for tid in out["cards"]:
                    row = board.execute(
                        "SELECT summary FROM task_runs WHERE task_id = ? AND summary IS NOT NULL "
                        "AND summary != '' ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
                    if row:
                        out["cards"][tid]["summary"] = row[0]
        board.close()
    except sqlite3.Error as exc:
        out["error"] = "kanban board: %s" % exc
print(SENTINEL)
print(json.dumps(out))
"""


def command(task_ids: list[str]) -> str:
    """The ``sh -c`` line that reads ``task_ids``' statuses in the pod."""
    args = " ".join(
        shlex.quote(a) for a in [DATA_ROOT, BOARD_FILE, BOARD_PRESENT, *task_ids]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_IN_POD_SCRIPT)} {args}'
    )


def read_statuses(
    shell: Callable[[str, float], str], task_ids: list[str], timeout: float
) -> dict[str, str] | None:
    """The board's current status for each of ``task_ids``, or ``None``.

    ``shell`` is :func:`harness._agent_shell`, taken as a parameter so this
    module stays importable without the harness and testable with a canned
    reply.

    ``None`` means the read cannot be trusted: no card was asked for, the
    script did not run to completion, its reply was not JSON, or the board
    could not be opened. A card the board does not know is simply absent from
    the returned map; the caller decides what an unknown card means.
    """
    if not task_ids:
        return None
    reply = shell(command(task_ids), timeout)
    marker = reply.find(BOARD_PRESENT)
    if marker < 0:
        _log.debug("kanban board could not be read for %s", ", ".join(task_ids))
        return None
    body = reply[marker + len(BOARD_PRESENT) :].strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        _log.warning("kanban board reply is not JSON: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        _log.warning("kanban board: %s", payload["error"])
        return None
    statuses = payload.get("statuses")
    if not isinstance(statuses, dict):
        return None
    return {str(k): str(v) for k, v in statuses.items() if k in task_ids}


def api_session_id(context_id: str) -> str:
    """The Hermes session the bridge's api executor runs ``context_id``'s turns in.

    ``""`` for a contextId the bridge would hash rather than use verbatim (over
    :data:`API_CONTEXT_ID_MAX_LEN`, or not path-safe). The gateway never mints
    one, so this side does not mirror the hash; the caller treats ``""`` as no
    session and waits the way it always has.
    """
    if len(context_id) <= API_CONTEXT_ID_MAX_LEN and _API_SAFE_CONTEXT_ID.match(context_id):
        return API_SESSION_PREFIX + context_id
    return ""




@dataclass
class SessionCards:
    """One read of a session's cards: which it filed, and where each stands.

    ``session_found`` is whether the store knows the session at all -- false
    on an install whose turns do not run there (the bridge's ``cli``
    executor), which is the caller's cue to wait the way it always has.
    ``card_ids`` are the cards the session filed, from its ``kanban_create``
    results when the store kept them (``source`` ``"state.db"``), else from
    the board's subscriptions addressed to it (``"kanban_notify_subs"``).
    ``cards`` maps each id the board knows to its ``status``, ``result``
    (``tasks.result``) and ``summary`` (the newest non-empty
    ``task_runs.summary``).
    """

    session_found: bool
    card_ids: list[str] = field(default_factory=list)
    source: str = ""
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_shown(self, task_id: str) -> dict[str, Any] | None:
        """``task_id`` as one ``kanban_show`` tool entry, or ``None`` if unknown.

        The shape ``parsing.reported_statuses`` and ``parsing.delivered_results``
        read -- ``{"task": {id, status, result}, "runs": [{summary}]}`` -- so
        the wait's settle grades a card read off the board exactly as it grades
        one an agent read back on a status turn. The caller reads back only a
        card whose status is terminal: a card still moving can carry an earlier
        run's summary or a stashed result that is not its answer.
        """
        card = self.cards.get(task_id)
        if card is None:
            return None
        payload: dict[str, Any] = {
            "task": {"id": task_id, "status": card.get("status"), "result": card.get("result")},
            "runs": [{"summary": card["summary"]}] if card.get("summary") else [],
        }
        return {
            "name": "kanban_show",
            "args": {"task_id": task_id},
            "result": json.dumps(payload),
            "status": "completed",
        }


def session_cards_command(session_id: str) -> str:
    """The ``sh -c`` line that reads ``session_id``'s cards in the pod."""
    args = " ".join(
        shlex.quote(a)
        for a in [
            DATA_ROOT,
            BOARD_FILE,
            STORE_FILE,
            SESSION_CARDS_PRESENT,
            session_id,
            DELEGATION_TOOL,
            str(MAX_SESSION_CARDS),
            str(MAX_CREATE_RESULT_CHARS),
            str(MAX_CHAIN_STEPS),
        ]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_SESSION_CARDS_SCRIPT)} {args}'
    )


def read_session_cards(
    shell: Callable[[str, float], str], session_id: str, timeout: float
) -> SessionCards | None:
    """The cards ``session_id`` filed and their board state, or ``None``.

    ``None`` is a read that cannot be trusted, by the same rule as
    :func:`read_statuses`: no sentinel, a reply that is not JSON, or a store
    or board that could not be opened.
    """
    if not session_id:
        return None
    reply = shell(session_cards_command(session_id), timeout)
    marker = reply.find(SESSION_CARDS_PRESENT)
    if marker < 0:
        _log.debug("the session store could not be read for %s", session_id)
        return None
    try:
        payload = json.loads(reply[marker + len(SESSION_CARDS_PRESENT) :].strip())
    except json.JSONDecodeError as exc:
        _log.warning("session cards reply is not JSON: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        _log.warning("session cards: %s", payload["error"])
        return None
    created = payload.get("created")
    subscribed = payload.get("subscribed")
    raw_cards = payload.get("cards")
    # The create results go through the same parser the other transports use,
    # so an id is accepted here by exactly the rule it is accepted there.
    ids = delegated_task_ids(
        [{"name": DELEGATION_TOOL, "result": c} for c in created if isinstance(c, str)]
        if isinstance(created, list)
        else []
    )
    source = "state.db" if ids else ""
    if not ids and isinstance(subscribed, list):
        ids = list(
            dict.fromkeys(t for t in subscribed if isinstance(t, str) and _TASK_ID_RE.match(t))
        )
        source = "kanban_notify_subs" if ids else ""
    cards: dict[str, dict[str, Any]] = {}
    if isinstance(raw_cards, dict):
        for tid, card in raw_cards.items():
            if tid in ids and isinstance(card, dict):
                cards[tid] = {
                    "status": str(card.get("status") or ""),
                    "result": card.get("result") if isinstance(card.get("result"), str) else None,
                    "summary": card.get("summary")
                    if isinstance(card.get("summary"), str)
                    else None,
                }
    return SessionCards(
        session_found=bool(payload.get("session")), card_ids=ids, source=source, cards=cards
    )
