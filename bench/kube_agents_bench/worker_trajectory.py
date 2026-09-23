"""Read the delegated workers' tool calls back into the run record.

A devops-bench run of a kube-agents case records only the front agent's tool
calls in ``results.json`` ``trajectory`` -- for a delegated case that is one
``kanban_create``. The work is done by the platform worker and, when it fans
out, by a per-cluster Cluster Agent, and neither appears in the record: a row
cannot say whether the agent read the cluster, what it read, whether it
delegated, or where its fix came from (#1729).

The transcripts exist on the install; they are just not collected. Every
worker is a hermes session in its profile's session store, and every tool call
it made is a row in that store's ``messages`` table. This module reads them
back, one ``kubectl exec`` per run, after the delegated work settles and
before :func:`harness._purge_card_state` and the per-run Cluster Agent profile
are gone.

How a card is mapped to its sessions, in this order, all inside the pod:

1. ``task_runs.metadata.worker_session_id`` -- hermes' own stamp, when the
   worker ran with ``HERMES_SESSION_ID`` set. The image's pinned hermes does
   not set it for dispatcher-spawned workers, so this is usually absent.
2. The dispatcher's prompt. Every worker is spawned as ``hermes -p <profile>
   chat -q "work kanban task <id>"`` (``kanban_db._default_spawn``), so the
   session whose first user message names the card is the card's session.
   Kanban sessions record no ``cwd`` (``run_agent._launch_cwd_for_session``),
   which is why the prompt is the key rather than the workspace path.

Cards are discovered from the front card down: the cards the front agent filed
(``awaited``), the cards each worker fanned out (``kanban_worker_children``,
the creator-of table the image adds in ``deploy/docker/patches/
kanban_children_settled.py``; upstream's ``task_links`` is a predecessor edge,
not ownership) and any continuation card gated on one of them. A Cluster
Agent's card is found this way, and its session store is
``/opt/data/profiles/<assignee>/state.db``, read before the profile is pruned.

Each tool call becomes one devops-bench trajectory entry in the same shape
``parsing.parse_response`` emits (``name`` / ``args`` / ``result`` /
``status``) plus four tags: ``agent`` (the profile that made the call),
``task`` (the card it was working), ``session`` and ``at`` (epoch seconds).
Results and arguments are scrubbed and clipped inside the pod, in that order,
so nothing credential-shaped leaves it and a chatty worker cannot make the exec
output unbounded. The scrubber is the install's own ``AuditRedactor`` (the one
the audit log and the LiteLLM gateway run every message through), loaded by
path from the image (:data:`REDACTOR_PATH`), plus a supplement for what it does
not cover: a Secret's JSON ``data`` object, the continuation lines of a block
scalar under ``data:``, and userinfo in a URL. Blanking is by shape, not by ``kind``: a ConfigMap's
``data:`` is blanked as a Secret's is, the call the redactor documents as the
safe direction to err in. A pod whose redactor cannot be loaded withholds every
result and argument rather than sending them unscrubbed; the call names, tags
and statuses still come back. The tags are also how ``tool_called`` keeps its
router-only contract: it skips every entry carrying ``agent``.

Two readers change with this. The record's ``trajectory`` is what devops-bench
hands its judged metrics as the execution trace, so the judge now sees the
worker's steps beside the router's ``kanban_create`` -- which is what the
record is for; the gated judged rung compares ``OutcomeValidity`` only, which
reads the answer, not the trace. And ``metadata["worker_trajectory"]`` on the
``AgentResult`` carries the card-to-session map the read used, for the harness
log and the tests: devops-bench does not copy ``metadata`` into
``results.json``, so on disk the entries themselves are the record and a read
problem is a harness-log warning.

Capture is best effort in the same sense as the artifact read-back: a pod that
cannot be reached costs the record its worker transcript, never the run. The
difference between "nothing to read" and "could not read" is kept, as
``_worker_commands`` keeps it: the in-pod script prints a sentinel before its
JSON, and a reply without it is reported as ``None``.
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kube_agents_bench.parsing import _call_args

__all__ = ["WorkerCapture", "capture"]

_log = logging.getLogger("kube_agents_bench.worker_trajectory")

# Line the in-pod script prints before its JSON. A reply without it means the
# script never ran to completion (no kubectl, no python, an unreachable pod),
# which is a capture failure rather than an empty capture.
CAPTURE_PRESENT = "__WORKER_TRAJECTORY__"

# The hermes data volume, the root ``harness._ATTACHMENTS_DIR`` and
# ``harness._LOGS_DIR`` hang off: ``kanban.db`` and the ``default`` profile's
# ``state.db`` sit in it, every other profile's under ``profiles/<name>/``.
# Passed to the in-pod script as an argument so the tests can point the same
# script at a temporary tree.
DATA_ROOT = "/opt/data"

# Interpreters to run the in-pod script under, in order: hermes' own venv,
# the one binary the image is guaranteed to carry, then whatever ``python3``
# resolves to on the container's PATH.
HERMES_PYTHON = "/opt/hermes/.venv/bin/python3"
FALLBACK_PYTHON = "python3"

# The install's own credential redactor: ``agents/chat/defaults/plugins/common/
# redactor.py``, which the image copies to ``/opt/defaults`` (deploy/docker/
# Dockerfile). Loaded by path inside the pod, so the record is scrubbed by the
# same rules and markers as the audit log and no copy of them lives here.
# Passed to the in-pod script as an argument so the tests can point it at the
# repository's file.
REDACTOR_PATH = "/opt/defaults/plugins/common/redactor.py"

# Bounds applied inside the pod, so the exec output stays bounded however much
# a worker talked. A run's workers make tens to a few hundred calls; the call
# cap is a runaway guard, and the per-field caps keep a ``kubectl logs`` dump or
# a manifest read from carrying the whole record with it. A clipped field ends
# in a marker naming how much was dropped. Clipping follows scrubbing, so a cut
# never lands inside a Secret block the scrubber then cannot see.
MAX_CARDS = 32
MAX_CALLS = 2000
MAX_RESULT_CHARS = 2000
MAX_ARGS_CHARS = 2000

# Runs inside the agent container under hermes' own interpreter. Plain
# ``python3`` and ``sqlite3``, plus the redactor loaded from the image: nothing
# from hermes is imported, so a hermes release that moves a module cannot break
# the read, only a schema change can -- and that lands in ``errors`` rather
# than in an exception. A redactor that fails to load lands there too, and
# withholds content rather than leaking it.
#
# Kept as one string rather than a file the harness would have to ship into
# the pod: ``_agent_shell`` runs ``sh -c``, and a quoted ``python3 -c``
# argument is the whole transport. Positional arguments carry everything the
# harness side also knows -- the data root, the sentinel, the caps -- and then
# the card ids, so no literal is spelled twice.
_IN_POD_SCRIPT = r"""
import importlib.util, json, os, re, sqlite3, sys

ROOT, SENTINEL, REDACTOR = sys.argv[1:4]
MAX_CARDS, MAX_CALLS, MAX_RESULT, MAX_ARGS = [int(a) for a in sys.argv[4:8]]
roots = [a for a in sys.argv[8:] if a]
# Seconds a read waits on a locked store before reporting the card unread. A
# hermes writer holds a WAL lock for milliseconds; anything longer is stuck.
SQLITE_BUSY_TIMEOUT = 10
out = {"cards": [], "calls": [], "errors": [], "truncated": False}
JSON_PREFIX = "\x00json:"
PROMPT = "work kanban task "
# The redactor's own marker, so a reader grepping artifacts finds one marker.
REDACTED = "[REDACTED_SECRET]"
WITHHELD = "[WITHHELD: redactor unavailable in the pod]"
TOOL_ERROR_PREFIX = "Error executing tool"

# What AuditRedactor leaves behind in the text a worker reads. In a Secret
# payload: the continuation lines of a block scalar under data:/stringData:
# (its line scan blanks key: value pairs only), and the JSON form,
# "data": {...}, closed or cut short. In free text: userinfo in a URL, which
# it has no pattern for, and a `NAME: value` / `NAME=value` line whose name
# its key-based walk would blank in a mapping but its text pattern does not
# (`AWS_SECRET_ACCESS_KEY:` in a `kubectl describe pod`, `client-key-data:`
# in a kubeconfig) -- the name test reuses the redactor's own key vocabulary.
YAML_BLOCK_RE = re.compile(r"^(\s*)(data|stringData)\s*:\s*$")
YAML_PAIR_RE = re.compile(r"^(\s*)([^\s:]+)\s*:\s*(.*)$")
JSON_BLOCK_RE = re.compile(r'("(?:data|stringData)"\s*:\s*\{)([^{}]*)(\}|\Z)')
JSON_PAIR_RE = re.compile(r'("(?:[^"\\]|\\.)*"\s*:\s*")((?:[^"\\]|\\.)*)("|\Z)')
URL_USERINFO_RE = re.compile(r"(://[^\s/:@]+:)[^\s/@]+(?=@)")
# The separator stays on the line, so `Environment:` above an indented env
# line does not swallow it; the value is the rest of the line, the safe side.
TEXT_KV_RE = re.compile(r"(?m)(?<![\w.\-/\"])([\w.\-]+)([ \t]*[:=][ \t]*)([^\n]+)")
KEY_DATA_WORDS = ({"key", "data"}, {"certificate", "data"})


def load_redactor():
    name = "kube_agents_audit_redactor"
    spec = importlib.util.spec_from_file_location(name, REDACTOR)
    if spec is None or spec.loader is None:
        raise ImportError("no loader for " + REDACTOR)
    module = importlib.util.module_from_spec(spec)
    # Registered before the exec: the file's dataclass resolves its string
    # annotations through sys.modules (credential_patterns in
    # scripts/validate_bench_cases.py explains the failure otherwise).
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.AuditRedactor


try:
    redactor = load_redactor()
except Exception as exc:
    out["errors"].append(
        "redactor %s: %s; results and arguments withheld" % (REDACTOR, exc)
    )
    redactor = None


def scrub_blocks(text):
    if "data" not in text:
        return text
    lines = text.split("\n")
    block = None
    for i, line in enumerate(lines):
        opener = YAML_BLOCK_RE.match(line)
        if opener:
            block = len(opener.group(1))
            continue
        if block is None or not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= block:
            block = None
            continue
        pair = YAML_PAIR_RE.match(line)
        if pair is None:
            lines[i] = " " * indent + REDACTED
        elif pair.group(3):
            lines[i] = "%s%s: %s" % (pair.group(1), pair.group(2), REDACTED)
    text = "\n".join(lines)
    return JSON_BLOCK_RE.sub(
        lambda m: m.group(1)
        + JSON_PAIR_RE.sub(lambda p: p.group(1) + REDACTED + p.group(3), m.group(2))
        + m.group(3),
        text,
    )


def named_value_is_sensitive(name):
    words = redactor._get_key_words(name)
    return bool(words & redactor.SENSITIVE_KEYS) or any(w <= words for w in KEY_DATA_WORDS)


def blank_named_values(text):
    return TEXT_KV_RE.sub(
        lambda m: m.group(1) + m.group(2) + REDACTED
        if named_value_is_sensitive(m.group(1))
        else m.group(0),
        text,
    )


def as_json(text):
    # A string that is itself a JSON document: a `terminal` result of
    # `kubectl get -o json`, a Secret's last-applied-configuration annotation,
    # the arguments hermes stores as a JSON string. Walked as a value, so
    # what is escaped inside it is seen unescaped, then re-serialised.
    if text.lstrip()[:1] not in "{[":
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def supplement(value, key=None):
    # The shapes the redactor lacks, applied where the strings sit so YAML
    # inside a JSON string field is seen with its newlines rather than as
    # escaped text; a data/stringData mapping is blanked wholesale, as the
    # text shapes are. AuditRedactor.redact then does the rest of the walk:
    # its patterns on every string, and its key-based blanking of anything
    # under a password/token/secret/credentials-named key.
    if isinstance(value, str):
        parsed = as_json(value)
        if parsed is not None:
            return json.dumps(scrub(parsed))
        text = URL_USERINFO_RE.sub(lambda m: m.group(1) + REDACTED, value)
        return blank_named_values(scrub_blocks(text))
    if isinstance(value, dict):
        if key in ("data", "stringData"):
            return {k: REDACTED if isinstance(v, str) else supplement(v) for k, v in value.items()}
        return {k: supplement(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [supplement(v) for v in value]
    return value


def scrub(value):
    if redactor is None:
        return WITHHELD
    return redactor.redact(supplement(value))


def failed(result):
    # parsing._output_failed, on the result before it is clipped: a clipped
    # JSON failure no longer parses, so the verdict has to be taken here.
    if isinstance(result, str):
        if result.startswith(TOOL_ERROR_PREFIX):
            return True
        try:
            result = json.loads(result.strip())
        except ValueError:
            return False
    if not isinstance(result, dict):
        return False
    if result.get("success") is False or result.get("ok") is False:
        return True
    code = result.get("exit_code", result.get("returncode"))
    if isinstance(code, int) and code != 0:
        return True
    return bool(result.get("error")) and not (
        result.get("content") or result.get("result") or result.get("structuredContent")
    )


def clip(text, limit):
    if text is None:
        return None
    text = text if isinstance(text, str) else json.dumps(text, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + " ...[clipped %d chars]" % (len(text) - limit)


def decode(content):
    if isinstance(content, str) and content.startswith(JSON_PREFIX):
        try:
            content = json.loads(content[len(JSON_PREFIX):])
        except ValueError:
            return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return content


def ro(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=SQLITE_BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def state_db(profile):
    if not profile or profile == "default":
        return ROOT + "/state.db"
    return ROOT + "/profiles/" + profile + "/state.db"


def like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def has_table(conn, name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def parse_calls(raw):
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return calls if isinstance(calls, list) else []


def read_session(conn, card, profile, sid):
    seen = set()
    pending = []
    count = 0
    orphans = 0
    rows = conn.execute(
        "SELECT id, role, content, tool_name, tool_calls, tool_call_id, timestamp "
        "FROM messages WHERE session_id = ? ORDER BY id",
        (sid,),
    )
    for row in rows:
        key = (row["role"], row["content"], row["timestamp"], row["tool_call_id"],
               row["tool_calls"], row["tool_name"])
        if key in seen:
            continue
        seen.add(key)
        if row["role"] == "assistant" and row["tool_calls"]:
            for tc in parse_calls(row["tool_calls"]):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                if len(out["calls"]) >= MAX_CALLS:
                    out["truncated"] = True
                    return count
                arguments = fn.get("arguments")
                entry = {
                    "name": str(fn.get("name") or ""),
                    "args": clip(scrub(arguments), MAX_ARGS) if arguments is not None else None,
                    "result": None,
                    "failed": None,
                    "agent": profile,
                    "task": card,
                    "session": sid,
                    "at": row["timestamp"],
                    "_id": tc.get("id") or tc.get("call_id"),
                }
                pending.append(entry)
                out["calls"].append(entry)
                count += 1
        elif row["role"] == "tool":
            # parse_response's rule: an id is matched to its call or nothing,
            # never to an unrelated pending call; a result without an id goes
            # to the oldest unanswered call that has none, by name when the
            # row names one.
            target = None
            if row["tool_call_id"]:
                target = next((e for e in pending if e["_id"] == row["tool_call_id"]), None)
            else:
                unkeyed = [e for e in pending if e["_id"] is None]
                if row["tool_name"]:
                    target = next((e for e in unkeyed if e["name"] == row["tool_name"]), None)
                if target is None and unkeyed:
                    target = unkeyed[0]
            if target is None:
                orphans += 1
                continue
            pending.remove(target)
            result = decode(row["content"])
            target["failed"] = failed(result)
            target["result"] = clip(scrub(result), MAX_RESULT)
    if orphans:
        out["errors"].append(
            "session %s of %s: %d tool result(s) matched no call" % (sid, profile, orphans)
        )
    return count


def sessions_for(card, assignee, runs):
    found = []
    for run in runs:
        try:
            md = json.loads(run["metadata"] or "{}")
        except ValueError:
            md = {}
        sid = md.get("worker_session_id") if isinstance(md, dict) else None
        if isinstance(sid, str) and sid and sid not in [f[0] for f in found]:
            found.append((sid, run["profile"] or assignee, "run_metadata"))
    profiles = list(dict.fromkeys([r["profile"] or assignee for r in runs] + [assignee]))
    for profile in profiles:
        if not profile:
            continue
        path = state_db(profile)
        if not os.path.exists(path):
            out["errors"].append("no session store for profile %s" % profile)
            continue
        try:
            conn = ro(path)
            rows = conn.execute(
                "SELECT session_id, content FROM messages WHERE role = 'user' "
                "AND content LIKE ? ESCAPE '\\' ORDER BY id",
                ("%" + PROMPT + like_escape(card) + "%",),
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            out["errors"].append("session store for %s: %s" % (profile, exc))
            continue
        # LIKE's trailing wildcard would also take a card whose id merely
        # starts with this one; the id has to end where the prompt's does.
        exact = re.compile(re.escape(PROMPT + card) + r"(?![\w-])")
        for row in rows:
            sid = row["session_id"]
            if exact.search(row["content"] or "") and sid not in [f[0] for f in found]:
                found.append((sid, profile, "worker_prompt"))
    return found


try:
    kb = ro(ROOT + "/kanban.db")
except sqlite3.Error as exc:
    out["errors"].append("kanban board: %s" % exc)
    kb = None

cards = {}
queue = list(roots)
while kb is not None and queue:
    tid = queue.pop(0)
    if tid in cards:
        continue
    if len(cards) >= MAX_CARDS:
        out["truncated"] = True
        break
    try:
        row = kb.execute(
            "SELECT id, assignee, status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        if row is None:
            out["errors"].append("card %s is not on the board" % tid)
            cards[tid] = None
            continue
        runs = kb.execute(
            "SELECT id, profile, status, started_at, ended_at, metadata "
            "FROM task_runs WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        children = []
        if has_table(kb, "kanban_worker_children"):
            children += [
                r["child_id"]
                for r in kb.execute(
                    "SELECT child_id FROM kanban_worker_children WHERE creator_id = ? "
                    "ORDER BY created_at, child_id",
                    (tid,),
                )
            ]
        children += [
            r["child_id"]
            for r in kb.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
                (tid,),
            )
        ]
    except sqlite3.Error as exc:
        out["errors"].append("card %s: %s" % (tid, exc))
        cards[tid] = None
        continue
    card = {
        "task": tid,
        "assignee": row["assignee"],
        "status": row["status"],
        "runs": len(runs),
        "children": list(dict.fromkeys(children)),
        "sessions": [],
    }
    cards[tid] = card
    queue.extend(card["children"])
    for sid, profile, match in sessions_for(tid, row["assignee"], runs):
        try:
            conn = ro(state_db(profile))
            calls = read_session(conn, tid, profile, sid)
            conn.close()
        except sqlite3.Error as exc:
            out["errors"].append("session %s of %s: %s" % (sid, profile, exc))
            continue
        card["sessions"].append(
            {"id": sid, "agent": profile, "match": match, "calls": calls}
        )
    if not card["sessions"] and runs:
        out["errors"].append("no session found for card %s" % tid)

out["cards"] = [c for c in cards.values() if c is not None]
for entry in out["calls"]:
    entry.pop("_id", None)
print(SENTINEL)
print(json.dumps(out, default=str))
"""


@dataclass
class WorkerCapture:
    """What one run's delegated workers did, ready for the run record.

    Attributes:
        entries: Trajectory entries in call order per session, each the
            canonical ``name`` / ``args`` / ``result`` / ``status`` shape plus
            ``agent`` / ``task`` / ``session`` / ``at`` tags. Appended to
            ``AgentResult.trajectory`` after the front agent's own calls.
        summary: The card-to-session map and any per-card read problems --
            no session found, a profile's store missing, the read clipped.
            Kept on the ``AgentResult``'s ``metadata["worker_trajectory"]``
            for the harness log and the tests; devops-bench does not write
            ``metadata`` to the record, so it is not in ``results.json``.
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def _entry(call: dict[str, Any]) -> dict[str, Any]:
    """One trajectory entry in ``parsing.parse_response``'s shape, plus the tags.

    ``status`` comes from the pod's ``failed`` verdict, taken there on the
    result before it was clipped (a clipped JSON failure would not parse
    here). ``args`` follow ``parsing._call_args``: a JSON object as is,
    anything else -- a clipped or withheld string included -- under ``raw``.
    """
    result = call.get("result") if isinstance(call.get("result"), str) else None
    if result is None:
        status = "called"
    elif call.get("failed"):
        status = "error"
    else:
        status = "completed"
    return {
        "name": str(call.get("name") or ""),
        "args": _call_args(call.get("args")),
        "result": result,
        "status": status,
        "agent": str(call.get("agent") or ""),
        "task": str(call.get("task") or ""),
        "session": str(call.get("session") or ""),
        "at": call.get("at"),
    }


def command(task_ids: list[str]) -> str:
    """The ``sh -c`` line that runs the in-pod read for ``task_ids``.

    :data:`HERMES_PYTHON` first -- it is the one binary guaranteed to be in
    the image, whatever the base image puts on ``PATH`` -- then
    :data:`FALLBACK_PYTHON`.
    """
    args = " ".join(
        shlex.quote(a)
        for a in [
            DATA_ROOT,
            CAPTURE_PRESENT,
            REDACTOR_PATH,
            str(MAX_CARDS),
            str(MAX_CALLS),
            str(MAX_RESULT_CHARS),
            str(MAX_ARGS_CHARS),
            *task_ids,
        ]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_IN_POD_SCRIPT)} {args}'
    )


def capture(
    shell: Callable[[str, float], str], task_ids: list[str], timeout: float
) -> WorkerCapture | None:
    """Read the delegated workers' tool calls for ``task_ids`` through ``shell``.

    ``shell`` is :func:`harness._agent_shell` -- the same ``kubectl exec`` the
    artifact read-back and the card purge use -- taken as a parameter so this
    module stays importable without the harness and testable with a canned
    reply.

    Returns ``None`` when nothing was captured: no card was delegated, or the
    in-pod script did not run to completion. An empty ``entries`` with a
    populated ``summary`` means the read ran and the workers made no tool call
    the stores know of; ``summary["errors"]`` says what could not be read.
    """
    if not task_ids:
        return None
    reply = shell(command(task_ids), timeout)
    marker = reply.find(CAPTURE_PRESENT)
    if marker < 0:
        _log.warning("worker trajectory for %s could not be read", ", ".join(task_ids))
        return None
    body = reply[marker + len(CAPTURE_PRESENT) :].strip()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        _log.warning("worker trajectory for %s is not JSON: %s", ", ".join(task_ids), exc)
        return None
    if not isinstance(payload, dict):
        return None
    calls = payload.get("calls")
    entries = [_entry(c) for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []
    summary = {
        "cards": payload.get("cards") or [],
        "errors": [str(e) for e in payload.get("errors") or []],
        "truncated": bool(payload.get("truncated")),
        "calls": len(entries),
    }
    for problem in summary["errors"]:
        _log.warning("worker trajectory: %s", problem)
    return WorkerCapture(entries=entries, summary=summary)
