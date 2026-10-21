"""Refuse "done" on a fleet-audit card whose run published no ledger.

Installed into the image at ``/opt/hermes/tools/kanban_audit_ledger.py`` and
wired into ``tools/kanban_tools.py`` by
``deploy/docker/patches/apply_kanban_audit_ledger.py``.

Why
---
A fleet-audit stream publishes its report as a ledger issue:
``audit_report.py start`` opens the run and prints one JSON line with
``workspace`` and ``findings_path``, and ``finish`` publishes it and prints one
JSON line with ``status`` and ``issue_url``. The skill says a card whose audit
produced no ledger must not close ``done`` (``fleet-audit/references/on-demand.md``), but
that is an instruction only. Issue #2856 measured the cost: on the
``compliance-rbac-overgrant`` case more than half of the repetitions closed
the card with a chat summary ("audit complete, 24 findings") and no ledger.
The worker ran ``start``, then stopped when ``collect.py`` timed out, or ran
``finish`` and did not read its rejection, or started ``finish`` in the
background and never read it, and then called ``kanban_complete`` anyway. The
user was told the audit was done, sometimes that the ledger was published, and
no issue existed.

What this changes
-----------------
At ``kanban_complete`` the gate reads this card's own worker session from
Hermes's session store and looks in the tool outputs, in order, for the result
lines of ``start`` and ``finish``. It reads the result lines, not the exit
codes, so a chained command neither hides a run nor ends one; a grep of the
skill prints no result line. Only the output of a terminal or
``execute_code`` call that runs ``audit_report.py``, or of a
``process_manage`` read-back of a background job that such a call started, or
of Hermes's completion notice for such a job (a ``user`` row that holds that
one notice and nothing joined to it), is read, so a line that only looks like a result line, in the output of a
separate ``kubectl`` call (foreground or background) or a session search,
neither ends a run nor puts its URL in chat. A result line that repeats with the same tool call id is ignored,
because Hermes's in-place compaction stores a copy of the rows it keeps, ids
and content unchanged, after the rows it archives.

* No ``start`` result line: not an audit run. Nothing changes.
* Every ``start`` has a ``finish`` result line after it: accepted. Each ledger
  URL a ``finish`` printed that ``result`` does not contain is appended (a
  stream run once per repository prints one per repository). A null
  ``issue_url`` (``NO_FORGE``, or a ``CLEAN`` run with no earlier ledger) has
  no URL to give.
* The last ``start`` has no ``finish`` result line after it: refused once,
  with the next step. The submitted ``result`` is saved on the card first,
  marked "Not published", as ``kanban_children_settled`` saves its own, so a
  run that ends after the refusal does not lose the report and does not leave
  a claim of a ledger on the card.
* The retry, still with no ``finish`` result line: accepted, with ``result``
  prefixed by a "Not published" line. The card never wedges, and the person
  who asked reads that no ledger exists instead of a report that implies one.

Why the session store
---------------------
``audit_report.py`` runs in the agent's shell. On a sandboxed install that is
the sandbox pod, whose ``/opt/data`` is a separate volume, so the run's lease
and report files are not visible from the gateway pod where this handler runs
(``docs/designs/fleet-audit-report-store.md``). What the worker process does
see is every tool call and its result, and Hermes writes each tool round to
``state.db`` before the next model call. The worker's session id is
``HERMES_SESSION_ID``, rebound to the child session after a compression;
this gate also walks compression parents and children, so a long audit turn
that compressed is read whole.

Scope and failure
-----------------
A ``finish`` that a delegated subagent runs is recorded in the child's
session, not this card's, so it is not read: the skill has the worker run
``start`` and ``finish`` itself, and a subagent's summary is model text that
this gate does not trust.
The gate runs only for the caller's own card (``HERMES_KANBAN_TASK`` equal to
the card id). A gateway session closing someone else's card, the CLI and the
scheduler are not affected, and a scheduled audit is a cron run with no card.
Every read fails open: a missing or unreadable store, an unknown schema, or a
``audit_report.py finish`` output after the last ``start`` (or a read-back of a
``finish`` job) that Hermes replaced with a preview and a file path (its
per-result size limit) lets the completion through without a refusal, because
the ``finish`` line may be in the part that was moved; a warning line records
this case. When the refused result cannot be saved on the card,
the completion is accepted at once, marked "Not published".
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from contextlib import closing

logger = logging.getLogger(__name__)

#: The env var the dispatcher pins into every worker it spawns: the id of the
#: card the worker is running.
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"

#: The worker's session id. Read through ``gateway.session_context`` when it
#: exists, because a compression rebinds the ContextVar there, not os.environ.
SESSION_ID_ENV = "HERMES_SESSION_ID"

HERMES_HOME_ENV = "HERMES_HOME"
STATE_DB_NAME = "state.db"
DB_TIMEOUT_SECONDS = 2.0

#: ``sessions.end_reason`` of a session that a compression continued in a
#: child (the value ``kanban_event_routing`` walks on).
COMPRESSION_END_REASON = "compression"
#: Upper bound on the compression links walked in each direction.
COMPRESSION_WALK_MAX = 32

#: Prefix ``hermes_state`` puts on list or dict message content it stored as
#: JSON (``SessionDB._CONTENT_JSON_PREFIX``).
CONTENT_JSON_PREFIX = "\x00json:"

#: The tag Hermes puts on a tool result it replaced with a preview and a file
#: path (``tools/tool_result_storage.py`` ``PERSISTED_OUTPUT_TAG``).
PERSISTED_OUTPUT_TAG = "<persisted-output>"

#: How long a refusal stays on file: the same window as the other completion
#: gates (``DEFAULT_CLAIM_TTL_SECONDS`` in ``hermes_cli/kanban_db.py``).
NUDGE_TTL_SECONDS = 15 * 60

#: The keys of the one JSON line ``start`` prints.
START_KEYS = ("workspace", "findings_path")
#: The keys of the one JSON line ``finish`` prints, published or not.
FINISH_KEYS = ("status", "issue_url")
#: The stream a ``start`` line names, read from its ``findings_path``
#: (``audit_report.py`` ``findings_path_for``).
AUDIT_ID_RE = re.compile(r"findings_([A-Za-z0-9][A-Za-z0-9_-]*)\.json$")
#: The prefix of a real ledger URL. A placeholder such as the skill's ``"…"``
#: example is not one.
URL_PREFIXES = ("https://", "http://")
#: Name used when a ``start`` line names no stream.
UNKNOWN_AUDIT = "fleet"
#: A command whose output may carry a ``start`` or ``finish`` result line.
AUDIT_SCRIPT = "audit_report.py"
#: The tool that reads back the output of a background job: ``process_manage``
#: in the pinned Hermes, ``process`` in older releases (still a dispatch alias).
PROCESS_TOOLS = ("process_manage", "process")
#: The tool whose arguments are code rather than a ``command``: read whole.
CODE_TOOL = "execute_code"
#: The key that names a background job: in the result of the call that
#: backgrounded it, and in the arguments of the read-back that reads it.
JOB_ID_KEY = "session_id"
#: How Hermes reports a background job that ended: a ``user`` row that starts
#: with this text and the job id, then ``Command: <cmd>`` and ``Output:`` lines,
#: and a closing ``]`` after the output (``tools/process_registry_notifications.py``).
#: One row can hold several notices: a ``chat -q`` worker joins every notice
#: drained in a round with a blank line (``hermes_cli/quiet_single_query.py``).
NOTICE_PREFIX = "[IMPORTANT: Background process"
NOTICE_SEPARATOR = "\n\n" + NOTICE_PREFIX
#: Any block Hermes can join after a notice starts like this: another notice,
#: an async-delegation result, or a format added later. The guard keys on it,
#: not on one block's prefix, so a new format cannot slip past it.
JOINED_BLOCK_MARK = "\n\n["
NOTICE_JOB_RE = re.compile(re.escape(NOTICE_PREFIX) + r" (\S+)")
NOTICE_OUTPUT_MARK = "\nOutput:\n"
NOTICE_END = "]"
#: A command whose output could carry the ``finish`` result line.
FINISH_COMMAND_RE = re.compile(r"audit_report\.py\b[^\n]*\bfinish\b")
#: Read-only URI for the session store, so this reader never creates the file
#: or takes a write lock.
STATE_DB_URI = "file:{path}?mode=ro"

_MESSAGES_SQL = (
    "SELECT id, role, content, tool_call_id, tool_calls FROM messages "
    "WHERE session_id IN ({marks}) AND role IN ('assistant', 'tool', 'user') ORDER BY id"
)
_PARENT_SQL = (
    "SELECT p.id FROM sessions s JOIN sessions p ON p.id = s.parent_session_id "
    "WHERE s.id = ? AND p.end_reason = ?"
)
_CHILD_SQL = (
    "SELECT c.id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id "
    "WHERE p.id = ? AND p.end_reason = ? ORDER BY c.started_at"
)
_STASH_SQL = "UPDATE tasks SET result = ? WHERE id = ? AND status = 'running'"

REFUSAL = (
    "This card started a {audit} audit (`audit_report.py start`), but no "
    "`finish` result line came back after it, so its ledger is not published "
    "and this report has no ledger URL. Your result is saved on the card, "
    "marked not published. Run `audit_report.py finish` for {audit} in the "
    "foreground, not as a background process, and read the JSON line it "
    "prints. If it prints an `issue_url`, call kanban_complete again with that "
    "URL in `result`. If `finish` exits 2, fix what its error names and run "
    "`finish` again; do not run `start` again. If the audit cannot publish, "
    "call kanban_block with the reason instead of kanban_complete."
)

NOT_PUBLISHED_PREFIX = (
    "⚠️ **Not published:** the {audit} audit ran `start`, but no `finish` "
    "published its ledger, so no ledger issue exists for this run.\n\n"
)

LEDGER_LINE = "\n\nLedger: {url}"

#: Cards refused once and not yet completed, against ``time.monotonic()`` at
#: the refusal. Popped by the completion that answers them; see
#: ``kanban_result_required`` for why the memory must expire.
_refused_at: dict[str, float] = {}


def _session_env(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        return get_session_env(name, "") or ""
    except Exception:  # not inside Hermes (tests), or a Hermes without it
        return os.environ.get(name, "")


def state_db_path() -> str:
    """Hermes's session store for this process's profile, or "" when unknown."""
    home = ""
    try:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home() or "")
    except Exception:  # not inside Hermes (tests), or a Hermes without it
        home = os.environ.get(HERMES_HOME_ENV, "").strip()
    return os.path.join(home, STATE_DB_NAME) if home else ""


def _lineage(conn, session_id: str) -> list[str]:
    """``session_id`` plus the sessions it continues and is continued by."""
    found = [session_id]
    for sql in (_PARENT_SQL, _CHILD_SQL):
        current = session_id
        for _ in range(COMPRESSION_WALK_MAX):
            row = conn.execute(sql, (current, COMPRESSION_END_REASON)).fetchone()
            if not row or not row[0] or row[0] in found:
                break
            current = row[0]
            found.append(current)
    return found


def _decode(content):
    """A stored tool result as Python data where it starts with JSON, else text.

    Hermes can append text after a tool's JSON result before it stores it
    (hints about the directories a command named), so the leading JSON value
    is read and anything after it is ignored.
    """
    if content is None:
        return ""
    if isinstance(content, bytes):
        content = content.decode("utf-8", "replace")
    text = str(content)
    if text.startswith(CONTENT_JSON_PREFIX):
        text = text[len(CONTENT_JSON_PREFIX):]
    try:
        value, _end = json.JSONDecoder().raw_decode(text.lstrip())
        return value
    except ValueError:
        return text


def _texts(value) -> list[str]:
    """Every string a stored tool result carries, outermost first."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [t for v in value.values() for t in _texts(v)]
    if isinstance(value, list):
        return [t for v in value for t in _texts(v)]
    return []


def _result_lines(value):
    """``(kind, value, line)`` events in a tool result, in order.

    ``kind`` is ``"start"`` (``value`` is the stream) or ``"finish"``
    (``value`` is the ledger URL or ``None``).
    """
    for text in _texts(value):
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if not isinstance(parsed, dict):
                continue
            if all(k in parsed for k in START_KEYS):
                match = AUDIT_ID_RE.search(str(parsed.get(START_KEYS[1]) or ""))
                yield "start", match.group(1) if match else UNKNOWN_AUDIT, line
            elif all(k in parsed for k in FINISH_KEYS):
                url = parsed.get(FINISH_KEYS[1])
                if url is None:
                    yield "finish", None, line
                elif isinstance(url, str) and url.startswith(URL_PREFIXES):
                    yield "finish", url, line


def _notices(content) -> list[tuple[str, str]]:
    """``(job id, output)`` of the one background-completion notice in a row.

    A row that does not start with a notice holds none. A row that holds a
    blank line followed by ``[`` anywhere is not read at all: Hermes joins
    every block it drains in a round that way (other notices, async-delegation
    results with a subagent's summary), and a job's own output can carry the
    same text, so a real split point cannot be told from a forged one. An
    honest ``finish`` whose output has a blank line before a bracketed log
    line is refused too; the refusal asks for a foreground run, and a
    read-back of the job is still read. Such a row credits no
    ``finish``; the proof then has to come from a foreground ``finish`` or a
    read-back of the job (fail closed).
    """
    text = content if isinstance(content, str) else ""
    if not text.startswith(NOTICE_PREFIX) or JOINED_BLOCK_MARK in text:
        return []
    job = NOTICE_JOB_RE.match(text)
    if not job or NOTICE_OUTPUT_MARK not in text:
        return []
    output = text.split(NOTICE_OUTPUT_MARK, 1)[1].rstrip()
    if output.endswith(NOTICE_END):
        output = output[: -len(NOTICE_END)]
    return [(job.group(1), output)]


def _calls(tool_calls) -> dict[str, tuple[str, str, str]]:
    """``{call id: (tool name, command, job id)}`` for one assistant message.

    ``job id`` is the background job a read-back names, else ``""``.
    """
    found = {}
    calls = _decode(tool_calls)
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict) or not call.get("id"):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        args = function.get("arguments")
        if isinstance(args, str):
            args = _decode(args)
        # The terminal's ``command``, or the whole of an ``execute_code`` call,
        # so code that runs the script is read too. Another tool's arguments
        # only mention the script (a session search for it), so they are not.
        name = str(function.get("name") or "")
        if isinstance(args, dict) and isinstance(args.get("command"), str):
            command = args["command"]
        elif name == CODE_TOOL:
            command = json.dumps(args) if isinstance(args, dict) else str(args or "")
        else:
            command = ""
        job = args.get(JOB_ID_KEY) if isinstance(args, dict) else None
        found[str(call["id"])] = (name, command, str(job) if job else "")
    return found


def audit_run(rows) -> dict:
    """What the tool results in ``rows`` say about this card's audit runs.

    ``rows`` are ``(id, role, content, tool_call_id, tool_calls)`` in id
    order. Returns ``started`` (a ``start`` result line was seen),
    ``open_audit`` (the stream of the last ``start`` when no ``finish`` result
    line follows it, else ``None``), ``urls`` (each ledger URL a ``finish``
    printed, in order) and ``hidden`` (a tool output after the last ``start``
    was replaced by a preview).
    """
    state = {"started": False, "open_audit": None, "urls": [], "hidden": False}
    calls: dict[str, tuple[str, str, str]] = {}
    jobs: dict[str, str] = {}  # background job id -> the audit command that started it
    seen: set[str] = set()  # (tool call id, line): a compaction copy keeps both
    for _row_id, role, content, tool_call_id, tool_calls in rows:
        if role == "assistant":
            calls.update(_calls(tool_calls))
            continue
        if role == "user":
            # A background job that ended (``notify_on_complete``) reports
            # here. Only a job this run backgrounded with an audit command is
            # read, under the command recorded when it started.
            parts = [(jobs[job], output) for job, output in _notices(content) if job in jobs]
        elif role == "tool":
            tool, command, job = calls.get(str(tool_call_id), ("", "", ""))
            if tool in PROCESS_TOOLS:
                # A read-back speaks for the job it reads, and only an audit
                # job's output can carry a result line.
                command = jobs.get(job, "")
            parts = [(command, _decode(content))]
        else:
            continue
        for command, decoded in parts:
            _read_part(state, seen, jobs, command, decoded, content, tool_call_id)
    return state


def _read_part(state, seen, jobs, command, decoded, content, tool_call_id) -> None:
    """Fold one output, a tool result or one completion notice, into ``state``."""
    if AUDIT_SCRIPT not in command:
        return
    if isinstance(decoded, dict) and decoded.get(JOB_ID_KEY):
        jobs[str(decoded[JOB_ID_KEY])] = command
    if (
        state["open_audit"]
        and PERSISTED_OUTPUT_TAG in str(content or "")
        and FINISH_COMMAND_RE.search(command)
    ):
        state["hidden"] = True
    for kind, value, line in _result_lines(decoded):
        copy_key = f"{tool_call_id}\n{line}"
        if copy_key in seen:
            continue
        seen.add(copy_key)
        if kind == "start":
            state["started"] = True
            state["open_audit"] = value
            state["hidden"] = False
        elif state["started"]:
            state["open_audit"] = None
            if value and value not in state["urls"]:
                state["urls"].append(value)


def _read_rows(state_db: str, session_id: str) -> list:
    with closing(
        sqlite3.connect(STATE_DB_URI.format(path=state_db), uri=True, timeout=DB_TIMEOUT_SECONDS)
    ) as conn:
        sessions = _lineage(conn, session_id)
        marks = ",".join("?" for _ in sessions)
        return conn.execute(_MESSAGES_SQL.format(marks=marks), sessions).fetchall()


def _stash_submitted_result(connect, task_id: str, text: str) -> None:
    """Save a refused completion's result on the running card, status untouched.

    Raises when nothing was saved; the caller then accepts the completion,
    because a refusal that has not saved what it refuses can lose the report.
    Only a ``running`` card is written, so a late call on a closed card cannot
    replace the result it closed with.
    """
    conn = connect()
    try:
        cursor = conn.execute(_STASH_SQL, (text, task_id))
        if getattr(conn, "in_transaction", False):
            conn.commit()
        if cursor.rowcount != 1:
            raise LookupError(f"{task_id} is not a running card")
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def require_audit_ledger(task_id, connect, result, state_db=None):
    """The completion gate. Returns ``(error, result_to_store)``.

    ``error`` is ``None`` when the completion may proceed with
    ``result_to_store``. ``connect`` is ``hermes_cli.kanban_db_connect.connect``,
    injected by the applier's trailer, used only to save a refused result.
    """
    key = str(task_id or "").strip()
    if not key or os.environ.get(WORKER_TASK_ENV) != key:
        return None, result
    session_id = _session_env(SESSION_ID_ENV).strip()
    path = state_db if state_db is not None else state_db_path()
    if not session_id or not path or not os.path.exists(path):
        return None, result
    refused_at = _refused_at.pop(key, None)
    nudge_spent = (
        refused_at is not None and time.monotonic() - refused_at <= NUDGE_TTL_SECONDS
    )
    try:
        run = audit_run(_read_rows(path, session_id))
    except Exception as exc:  # noqa: BLE001 — sqlite, schema, JSON: fail open
        logger.warning(
            "kanban_audit_ledger: could not read %s's session %s (allowing the "
            "completion): %r", key, session_id, exc,
        )
        return None, result
    if not run["started"]:
        return None, result
    text = "" if result is None else str(result)
    missing = [url for url in run["urls"] if url not in text]
    text += "".join(LEDGER_LINE.format(url=url) for url in missing)
    audit = run["open_audit"]
    if audit is not None and run["hidden"]:
        logger.warning(
            "kanban_audit_ledger: %s has a moved tool output after its %s start; "
            "allowing the completion without a finish result line", key, audit,
        )
    if audit is None or run["hidden"]:
        return None, text if missing else result
    marked = NOT_PUBLISHED_PREFIX.format(audit=audit) + text
    if nudge_spent:
        logger.warning(
            "kanban_audit_ledger: %s is completing the %s audit with no finish "
            "result on its retry; marking it not published", key, audit,
        )
        return None, marked
    try:
        _stash_submitted_result(connect, key, marked)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "kanban_audit_ledger: could not save %s's submitted result "
            "(allowing the completion, marked not published): %r", key, exc,
        )
        return None, marked
    _refused_at[key] = time.monotonic()
    return REFUSAL.format(audit=audit), result
