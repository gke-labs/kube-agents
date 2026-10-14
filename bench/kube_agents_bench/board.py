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
they settle. This module has three reads, each one ``kubectl exec`` of the kind
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
- :func:`read_descendants`, for the eval harness settling phase when a card
  reaches its delegation ceiling. It finds unsettled fan-out child cards
  (from ``kanban_worker_children``) filed by workers of stalled cards so they
  can be archived in the same pass.

Best effort in the same sense as the artifact read-back: a pod that cannot
be reached or a store that cannot be opened returns ``None`` for that read,
and the caller decides what to do without it. The api transport spends a
status turn. The inject one retries a read three times; if the first read of
a wait never succeeds it falls back to the reply with a line on the record's
errors, and if a later one never does the repetition is infrastructure. A
wrong reading is
worse than no reading, so each in-pod script prints a sentinel before its JSON
and a reply without it is a failed read, never an empty one.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from kube_agents_bench.parsing import _TASK_ID_RE, DELEGATION_TOOL, delegated_task_ids
from kube_agents_bench.worker_trajectory import DATA_ROOT, FALLBACK_PYTHON, HERMES_PYTHON

__all__ = [
    "DESCENDANTS_PRESENT",
    "MAX_DESCENDANTS_SCANNED",
    "SessionCards",
    "TERMINAL_STATUSES",
    "api_session_id",
    "command_descendants",
    "read_descendants",
    "read_session_cards",
    "read_statuses",
]

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

# How many of a session's card ids the read reports, with their statuses. It
# is not the wait's cap -- that is the harness's _MAX_AWAITED_TASKS, applied to
# this list pending-first -- but a guard on the exec output, set well above
# that cap so the pending-first order sees every card a real fan-out files and
# not only its first few: one id and one status word per card, a few kilobytes
# at the bound. Cards' deliverables are read only for the ids the caller names
# (``want``, at most its cap), so their size does not scale with the scan. A
# session past the bound has its total reported, so the overflow is not silent.
MAX_SESSION_IDS_SCANNED = 1024
# How many compression continuations the session read follows, the defensive
# bound the pinned hermes' own get_compression_chain uses
# (hermes_state_compression.py); a chain this deep is pathological.
MAX_CHAIN_STEPS = 100
# How many unsettled descendant card ids a single descendant read collects
# before truncating. Bounds the SQLite payload returned from the agent pod;
# the harness settle loop applies _MAX_DESCENDANT_ARCHIVE_ROUNDS to bound
# overall rounds and total cards archived.
MAX_DESCENDANTS_SCANNED = 64

# Terminal task statuses considered settled by the eval harness.
TERMINAL_STATUSES = frozenset({"done", "archived", "blocked", "failed", "cancelled"})


# Runs inside the agent container, read-only like the status read. Positional
# arguments: data root, board file, store file, sentinel, session id, the
# create tool's name, the id scan bound, the caller's cap on ``want``, the
# chain bound, then the ``want`` ids whose deliverables to read.
_SESSION_CARDS_SCRIPT = r"""
import json, sqlite3, sys

ROOT, BOARD, STORE, SENTINEL, SID, CREATE = sys.argv[1:7]
MAX_IDS, MAX_WANT, MAX_CHAIN_STEPS = (int(a) for a in sys.argv[7:10])
WANT = [a for a in sys.argv[10:] if a][:MAX_WANT]
SQLITE_BUSY_TIMEOUT = 10
# Rows per IN (...) query, under every sqlite's bound-parameter limit.
CHUNK = 500
JSON_PREFIX = "\x00json:"
out = {"session": False, "sessions": [], "created": [], "created_total": 0,
       "subscribed": [], "subscribed_total": 0, "cards": {}, "error": None}


def ro(name):
    return sqlite3.connect("file:%s/%s?mode=ro" % (ROOT, name), uri=True,
                           timeout=SQLITE_BUSY_TIMEOUT)


def has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def columns(conn, table):
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}


def created_id(content):
    # The create result's task_id, whatever its type; the caller validates it.
    if isinstance(content, str) and content.startswith(JSON_PREFIX):
        content = content[len(JSON_PREFIX):]
    try:
        parsed = json.loads(content) if isinstance(content, str) else content
    except ValueError:
        return None
    return parsed.get("task_id") if isinstance(parsed, dict) else None


def marks(values):
    return ",".join("?" for _ in values)


def chunks(values):
    return [values[i:i + CHUNK] for i in range(0, len(values), CHUNK)]


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
    seen = set()
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
            tid = created_id(content)
            if tid is None:
                continue
            key = json.dumps(tid)
            if key in seen:
                continue
            seen.add(key)
            out["created_total"] += 1
            if len(out["created"]) < MAX_IDS:
                out["created"].append(tid)
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
            query += " ORDER BY rowid"
            subscribed = [str(r[0]) for r in board.execute(query, out["sessions"])]
            out["subscribed_total"] = len(subscribed)
            out["subscribed"] = subscribed[:MAX_IDS]
        ids = list(out["subscribed"])
        for tid in out["created"]:
            if isinstance(tid, str) and tid and tid not in ids:
                ids.append(tid)
        # Every scanned card's status; a deliverable only for the ids wanted.
        for part in chunks(ids):
            for tid, status in board.execute(
                "SELECT id, status FROM tasks WHERE id IN (%s)" % marks(part), part
            ):
                out["cards"][str(tid)] = {"status": str(status), "result": None, "summary": None}
        wanted = [t for t in WANT if t in out["cards"]]
        if wanted:
            if "result" in columns(board, "tasks"):
                for tid, result in board.execute(
                    "SELECT id, result FROM tasks WHERE id IN (%s)" % marks(wanted), wanted
                ):
                    out["cards"][str(tid)]["result"] = result
            if has_table(board, "task_runs") and "summary" in columns(board, "task_runs"):
                for tid in wanted:
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


# Line the in-pod descendants script prints before its JSON.
DESCENDANTS_PRESENT = "__KANBAN_DESCENDANTS__"

_DESCENDANTS_SCRIPT = r"""
import json, re, sqlite3, sys

ROOT, BOARD, SENTINEL, MAX_ARG, EXCLUDE_ARG = sys.argv[1:6]
MAX_CARDS = int(MAX_ARG)
exclude = set(a for a in EXCLUDE_ARG.split(",") if a)
roots = [a for a in sys.argv[6:] if a]
SQLITE_BUSY_TIMEOUT = 10
out = {"descendants": [], "truncated": False, "skipped": 0, "dropped": 0, "error": None}
TERMINAL_STATUSES = ("done", "archived", "blocked", "failed", "cancelled")
TASK_ID_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,64}\Z")

def has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None

def columns(conn, name):
    return {r[1] for r in conn.execute("PRAGMA table_info(%s)" % name)}

try:
    conn = sqlite3.connect("file:%s/%s?mode=ro" % (ROOT, BOARD), uri=True, timeout=SQLITE_BUSY_TIMEOUT)
    # tasks is hermes's core table: a store without it is not a board, so the
    # read fails to None here rather than walking nothing and reporting [].
    conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
    has_children = has_table(conn, "kanban_worker_children")
    task_cols = columns(conn, "tasks")
    has_created_at = "created_at" in task_cols
    has_idempotency_key = "idempotency_key" in task_cols

    min_root_created_at = None
    if has_created_at and roots:
        placeholders = ",".join("?" for _ in roots)
        root_rows = conn.execute(
            "SELECT created_at FROM tasks WHERE id IN (%s)" % placeholders,
            tuple(roots),
        ).fetchall()
        times = [r[0] for r in root_rows if r[0] is not None]
        if times:
            min_root_created_at = min(times)

    query_cols = ["status"]
    if has_created_at:
        query_cols.append("created_at")
    if has_idempotency_key:
        query_cols.append("idempotency_key")
    task_query = "SELECT " + ", ".join(query_cols) + " FROM tasks WHERE id = ?"

    queue = list(roots)
    seen = set(roots)
    descendants = []
    skipped = 0
    dropped = 0
    truncated = False
    while queue and not truncated:
        curr = queue.pop(0)
        children = []
        if has_children:
            rows = conn.execute(
                "SELECT child_id FROM kanban_worker_children WHERE creator_id = ? ORDER BY created_at, child_id",
                (curr,),
            ).fetchall()
            for r in rows:
                cid = str(r[0]) if r[0] is not None else ""
                if cid:
                    children.append(cid)
        for child in children:
            if child in seen:
                continue
            seen.add(child)
            if child in exclude:
                queue.append(child)
                continue
            row = conn.execute(task_query, (child,)).fetchone()
            if row is None:
                queue.append(child)
                continue
            status = str(row[0]) if row[0] is not None else None
            col_idx = 1
            child_created_at = None
            if has_created_at:
                child_created_at = row[col_idx]
                col_idx += 1
            child_idem_key = None
            if has_idempotency_key:
                child_idem_key = row[col_idx]
                col_idx += 1

            is_predating = (
                min_root_created_at is not None
                and child_created_at is not None
                and child_created_at < min_root_created_at
            )
            is_keyed = bool(child_idem_key)
            if is_predating or is_keyed:
                if status not in TERMINAL_STATUSES:
                    skipped += 1
                continue

            queue.append(child)

            if status not in TERMINAL_STATUSES:
                if not TASK_ID_RE.match(child):
                    dropped += 1
                elif len(descendants) < MAX_CARDS:
                    descendants.append(child)
                else:
                    truncated = True
                    break
    conn.close()
    out["descendants"] = descendants
    out["truncated"] = truncated
    out["skipped"] = skipped
    out["dropped"] = dropped
except sqlite3.Error as exc:
    out["error"] = "kanban board: %s" % exc
print(SENTINEL)
print(json.dumps(out))
"""


def command_descendants(
    task_ids: list[str],
    max_cards: int = MAX_DESCENDANTS_SCANNED,
    exclude: Iterable[str] = (),
) -> str:
    """The ``sh -c`` line that reads ``task_ids``' descendants in the pod."""
    exclude_arg = ",".join(str(e) for e in exclude if e)
    args = " ".join(
        shlex.quote(a)
        for a in [
            DATA_ROOT,
            BOARD_FILE,
            DESCENDANTS_PRESENT,
            str(max_cards),
            exclude_arg,
            *task_ids,
        ]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_DESCENDANTS_SCRIPT)} {args}'
    )


def read_descendants(
    shell: Callable[[str, float], str],
    task_ids: list[str],
    timeout: float,
    max_cards: int = MAX_DESCENDANTS_SCANNED,
    exclude: Iterable[str] = (),
) -> list[str] | None:
    """The unsettled descendant card ids filed under ``task_ids``, or ``None``.

    Walks ``kanban_worker_children`` (fanned-out children created by workers)
    recursively, skipping cards that are already settled (statuses in
    :data:`TERMINAL_STATUSES`), no longer exist on the board (missing
    ``tasks`` row), predate the root card's creation, or carry an
    ``idempotency_key`` (idempotent cards that may be shared across parallel
    repetition runs). When a card is keyed or predates the root, its entire
    subtree is omitted from traversal so parallel runs are protected from
    having their children archived. Cards in ``exclude`` (e.g. already-attempted
    cards from earlier rounds) are traversed to find deeper descendants but
    omitted from the returned list so they do not consume scan slots.
    Non-conforming card IDs (failing :data:`_TASK_ID_RE`) are dropped in-pod
    without taking scan slots, and warned about.

    ``shell`` is :func:`harness._agent_shell`, taken as a parameter so this
    module stays importable without the harness and testable with a canned
    reply.

    ``None`` means the read cannot be trusted: the script did not run to
    completion, its reply was not JSON, or the board could not be opened.
    An empty list means the read succeeded and no unsettled descendants exist.
    """
    if not task_ids:
        return []
    reply = shell(command_descendants(task_ids, max_cards=max_cards, exclude=exclude), timeout)
    marker = reply.find(DESCENDANTS_PRESENT)
    if marker < 0:
        _log.debug("kanban board descendants could not be read for %s", ", ".join(task_ids))
        return None
    body = reply[marker + len(DESCENDANTS_PRESENT) :].strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        _log.warning("kanban board descendants reply is not JSON: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("error"):
        _log.warning("kanban board descendants: %s", payload["error"])
        return None
    descendants = payload.get("descendants")
    if not isinstance(descendants, list):
        return None
    valid = []
    dropped_harness = 0
    for d in descendants:
        if isinstance(d, str) and _TASK_ID_RE.match(d):
            valid.append(d)
        else:
            dropped_harness += 1
    dropped_raw = payload.get("dropped")
    dropped_in_pod = dropped_raw if isinstance(dropped_raw, int) else 0
    total_dropped = dropped_in_pod + dropped_harness
    if total_dropped:
        _log.warning(
            "kanban board descendants ignored %d non-conforming card id(s); their workers may still hold dispatcher slots",
            total_dropped,
        )
    result = valid
    skipped = payload.get("skipped")
    if isinstance(skipped, int) and skipped > 0:
        _log.warning(
            "kanban board descendants skipped %d keyed or pre-dating live child card(s); their workers may still hold dispatcher slots",
            skipped,
        )
    if payload.get("truncated"):
        _log.warning(
            "kanban board descendants truncated at %d cards; some fan-out workers may still hold slots",
            max_cards,
        )
    return result


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
    ``cards`` maps each id the board knows to its ``status``, and, for the
    ids the read was asked for (``want``), its ``result`` (``tasks.result``)
    and ``summary`` (the newest non-empty ``task_runs.summary``); ``None``
    for the rest. An id in ``card_ids`` and not in ``cards`` is one the board
    has no row for.
    """

    session_found: bool
    card_ids: list[str] = field(default_factory=list)
    source: str = ""
    cards: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: How many cards the source names in all, past :data:`MAX_SESSION_IDS_SCANNED`
    #: included; ``card_ids`` is at most that many.
    total: int = 0

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


def session_cards_command(session_id: str, want: Sequence[str] = (), max_want: int = 0) -> str:
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
            str(MAX_SESSION_IDS_SCANNED),
            str(max_want),
            str(MAX_CHAIN_STEPS),
            *want,
        ]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_SESSION_CARDS_SCRIPT)} {args}'
    )


def read_session_cards(
    shell: Callable[[str, float], str],
    session_id: str,
    timeout: float,
    *,
    want: Sequence[str] = (),
    max_want: int = 0,
) -> SessionCards | None:
    """The cards ``session_id`` filed and their board state, or ``None``.

    Every scanned card comes back with its status. Deliverables are read only
    for ``want``, at most ``max_want`` of them: the caller's cap over the
    cards it awaits (``harness._MAX_AWAITED_TASKS``), so a poll that needs
    only statuses reads none.

    ``None`` is a read that cannot be trusted, by the same rule as
    :func:`read_statuses`: no sentinel, a reply that is not JSON, or a store
    or board that could not be opened.
    """
    if not session_id:
        return None
    reply = shell(session_cards_command(session_id, want, max_want), timeout)
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
    # The create results' ids go through the same parser the other transports
    # use, so an id is accepted here by exactly the rule it is accepted there.
    ids = delegated_task_ids(
        [{"name": DELEGATION_TOOL, "result": json.dumps({"task_id": c})} for c in created]
        if isinstance(created, list)
        else []
    )
    source = "state.db" if ids else ""
    total = int(payload.get("created_total") or 0) if ids else 0
    if not ids and isinstance(subscribed, list):
        ids = list(
            dict.fromkeys(t for t in subscribed if isinstance(t, str) and _TASK_ID_RE.match(t))
        )
        source = "kanban_notify_subs" if ids else ""
        total = int(payload.get("subscribed_total") or 0) if ids else 0
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
        session_found=bool(payload.get("session")),
        card_ids=ids,
        source=source,
        cards=cards,
        total=max(total, len(ids)),
    )
