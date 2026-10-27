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

How a card is mapped to its sessions, in this order, all inside the pod and
through the shared reader's functions (the feed maps a card the same way):

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
output unbounded. The rules are the shared transcript reader's
(``agents/platform/scripts/worker_transcript.py``, loaded by path from the
image at :data:`SUPPLEMENT_PATH`), applied in the session-kv feed's order: the
install's own ``AuditRedactor`` (the one the audit log and the LiteLLM gateway
run every message through, loaded from :data:`REDACTOR_PATH`) first, then the
supplement for what it leaves -- block scalars under ``data:``/``stringData:``,
a ``data``/``stringData`` object in JSON text or in a parsed manifest, URL
userinfo, ``NAME: value`` lines, the CLI credential flags, the bearer-scheme
pair, and the secret-read detector under which the result of a call that
prints a secret's value is recorded as :data:`WITHHELD_READ_RESULT`, a
redacted result rather than an unread one. One copy, so a shape the feed masks is masked here
too; a new shape lands in that module and nowhere else. A manifest's ``data`` mapping
is blanked by shape, not by ``kind``, in YAML and JSON alike: a ConfigMap's is
blanked as a Secret's, the call the redactor documents as the safe direction
to err in, and a Secret kubectl's key-sorted JSON cut short before its
``kind`` is blanked as text. An API response's ``data`` is not a manifest's.
A pod whose redactor or rules module cannot be loaded withholds every result
and argument rather than sending them unscrubbed; the call names, tags and
statuses still come back when the redactor is the one missing; when the rules
module is missing nothing is read, since the card-to-session mapping is its too.
The tags are also how ``tool_called`` keeps its default router-only contract:
``scope: router`` skips every entry carrying ``agent`` and ``scope: workers``
counts only those.

The same read collects each worker session's token counts (#1870). A session's
usage is aggregated on its ``sessions`` row in the same five columns the
harness reads for the front door's session (``input_tokens``,
``cache_read_tokens``, ``cache_write_tokens``, ``reasoning_tokens``,
``output_tokens``); a hermes that keeps usage per model in
``session_model_usage`` is summed from there when the row is empty or missing. The
counts come back per session in hermes' column names, are summed per profile
in :attr:`WorkerCapture.tokens`, and the harness maps them into its buckets
and adds them to the run's totals once the front door's own row is in
(``harness._fold_worker_tokens``) -- a delegated run's ``tokens.total`` was
the router's spend alone before this, the smallest part of the run.

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
``_worker_logs`` keeps it: the in-pod script prints a sentinel before its
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

# The shared transcript rules: ``agents/platform/scripts/worker_transcript.py``,
# which the image copies to ``/opt/defaults/scripts`` beside the session-kv
# server that imports it. Loaded by path inside the pod, so the record is
# scrubbed by the same supplement, in the same order, as the feed's worker
# steps, and mapped to its sessions by the same prompt and store layout.
# Passed to the in-pod script as an argument, right after the redactor, so the
# tests can point it at the repository's file.
SUPPLEMENT_PATH = "/opt/defaults/scripts/worker_transcript.py"

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
# What ``gaps`` reports for a read the pod clipped, by the cap that fired: past
# ``MAX_CARDS`` later cards were never read, and past ``MAX_CALLS`` the walk
# goes on but later workers' calls are dropped. Either way a profile that
# worked only the unread part is absent from the capture without having been
# absent from the run.
TRUNCATED_GAP = "the read stopped at %d cards; later cards were not read" % MAX_CARDS
CALL_CAP_GAP = "the read stopped at %d tool calls; later calls were not read" % MAX_CALLS
CLIP_GAPS = {"cards": TRUNCATED_GAP, "calls": CALL_CAP_GAP}
# What the in-pod script puts in place of every result and argument when the
# redactor will not load (a store without the shared rules module reads no
# session at all). Named here too so a verifier can tell an unreadable capture
# from a real one; a test holds the two spellings together.
WITHHELD = "[WITHHELD: redactor unavailable in the pod]"
# What it puts in place of the one result of a call the shared rules'
# secret-read detector matches, the arguments and every other result readable.
# It starts with `[REDACTED_` so the verifiers read it as a recorded call whose
# output is redacted (as they read a `[REDACTED_SECRET]` in any output), never
# as the whole capture being unread; a test holds the two spellings together.
WITHHELD_READ_RESULT = "[REDACTED_RESULT: the call read a credential; its output is not recorded]"

# Runs inside the agent container under hermes' own interpreter. Plain
# ``python3`` and ``sqlite3``, plus the redactor and the shared transcript
# rules loaded from the image: nothing from hermes is imported, so a hermes
# release that moves a module cannot break the read, only a schema change can
# -- and that lands in ``errors`` rather than in an exception. A redactor or
# rules module that fails to load lands there too, and withholds content
# rather than leaking it.
#
# Kept as one string rather than a file the harness would have to ship into
# the pod: ``_agent_shell`` runs ``sh -c``, and a quoted ``python3 -c``
# argument is the whole transport. Positional arguments carry everything the
# harness side also knows -- the data root, the sentinel, the caps -- and then
# the card ids, so no literal is spelled twice.
_IN_POD_SCRIPT = r"""
import importlib.util, json, os, sqlite3, sys

ROOT, SENTINEL, REDACTOR, SUPPLEMENT = sys.argv[1:5]
MAX_CARDS, MAX_CALLS, MAX_RESULT, MAX_ARGS = [int(a) for a in sys.argv[5:9]]
roots = [a for a in sys.argv[9:] if a]
# Seconds a read waits on a locked store before reporting the card unread. A
# hermes writer holds a WAL lock for milliseconds; anything longer is stuck.
SQLITE_BUSY_TIMEOUT = 10
# The reply is written in flushed pieces no larger than this. Under GKE Sandbox
# (gVisor) a single write of more than 64 KiB to an exec session's stdout is
# cut at 64 KiB (measured 2026-10-09: one print of 200,000 bytes arrived as
# 65,537; 32 KiB flushed writes arrived whole), and one print of the JSON lost
# every capture longer than that to a parse error.
WRITE_CHUNK = 32768
out = {"cards": [], "calls": [], "errors": [], "unread": [], "truncated": False, "clipped": []}


# Which cap stopped the read; ``truncated`` alone cannot say.
def stopped_at(cap):
    out["truncated"] = True
    if cap not in out["clipped"]:
        out["clipped"].append(cap)


# A note that also means something the run did was not read. Every other
# entry in ``errors`` -- a withheld redactor, orphan tool results, the store
# of a name no run was dispatched to -- leaves the capture's worker tags
# whole, so only these can hide a profile that did work.
def unread(problem):
    out["errors"].append(problem)
    out["unread"].append(problem)

JSON_PREFIX = "\x00json:"
WITHHELD = "[WITHHELD: redactor unavailable in the pod]"
WITHHELD_READ_RESULT = "[REDACTED_RESULT: the call read a credential; its output is not recorded]"
TOOL_ERROR_PREFIX = "Error executing tool"
# hermes' usage columns, on the ``sessions`` row as aggregates and, in a
# hermes that attributes usage per model, on ``session_model_usage`` rows.
TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)

# The step kinds the shared rules tell apart: a call's arguments carry the
# shell line the continuation fold and the CLI flag rules read, a result the
# inline-object rule.
CALL_KIND = "tool_call"
RESULT_KIND = "tool_result"
# The names the two files are registered under in sys.modules.
REDACTOR_MODULE_NAME = "kube_agents_audit_redactor"
SUPPLEMENT_MODULE_NAME = "kube_agents_worker_transcript"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("no loader for " + path)
    module = importlib.util.module_from_spec(spec)
    # Registered before the exec: the file's dataclass resolves its string
    # annotations through sys.modules (credential_patterns in
    # scripts/validate_bench_cases.py explains the failure otherwise).
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


try:
    redactor = load_module(REDACTOR_MODULE_NAME, REDACTOR).AuditRedactor
except Exception as exc:
    out["errors"].append(
        "redactor %s: %s; results and arguments withheld" % (REDACTOR, exc)
    )
    redactor = None

# The shared transcript reader: the supplement rules and the card-to-session
# mapping. An image without it is an image whose rules this read cannot run.
try:
    wt = load_module(SUPPLEMENT_MODULE_NAME, SUPPLEMENT)
except Exception as exc:
    out["errors"].append("transcript rules %s: %s; no session read" % (SUPPLEMENT, exc))
    wt = None


def scrub(value, kind):
    # The feed's rules in the feed's order: AuditRedactor.redact, then the
    # supplement, on the text as hermes stored it (a structured result is
    # serialised first and walked as JSON inside).
    if value is None:
        return None
    if redactor is None:
        return WITHHELD
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return wt.scrub_step_text(redactor, kind, text, WITHHELD)


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
    return str(wt.profile_state_db_path(ROOT, profile))


def like_escape(value):
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def session_tokens(conn, profile, sid):
    # The session row first: it is what /api/sessions/<id> serves for the front
    # door, so the workers are counted by the same rule. A row whose counts are
    # all zero on a store that also keeps per-model usage is summed from that
    # table instead. A session that reaches here made model calls (it has
    # assistant rows), so all zeros after both reads is "not tracked", never
    # "free": it is reported and stays out of the record rather than in it as
    # zero, the same as a store with neither table.
    counts = None
    if has_table(conn, "sessions"):
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if set(TOKEN_COLUMNS) <= cols:
            row = conn.execute(
                "SELECT %s FROM sessions WHERE id = ?" % ", ".join(TOKEN_COLUMNS), (sid,)
            ).fetchone()
            if row is not None:
                counts = {c: int(row[c] or 0) for c in TOKEN_COLUMNS}
    if (counts is None or not any(counts.values())) and has_table(conn, "session_model_usage"):
        row = conn.execute(
            "SELECT %s FROM session_model_usage WHERE session_id = ?"
            % ", ".join("COALESCE(SUM(%s), 0)" % c for c in TOKEN_COLUMNS),
            (sid,),
        ).fetchone()
        if row is not None and any(row):
            counts = {c: int(row[i] or 0) for i, c in enumerate(TOKEN_COLUMNS)}
    if counts is not None and not any(counts.values()):
        counts = None
    if counts is None:
        out["errors"].append("session %s of %s: no token counts in the store" % (sid, profile))
    return counts


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
                    stopped_at("calls")
                    return count
                arguments = fn.get("arguments")
                entry = {
                    "name": str(fn.get("name") or ""),
                    "args": clip(scrub(arguments, CALL_KIND), MAX_ARGS) if arguments is not None else None,
                    "result": None,
                    "failed": None,
                    "agent": profile,
                    "task": card,
                    "session": sid,
                    "at": row["timestamp"],
                    "_id": tc.get("id") or tc.get("call_id"),
                    # A call that reads a secret's value has its result
                    # withheld, as the feed withholds it; the verdict is
                    # still taken on the raw result. (`sessions_for` reads no
                    # session without the rules module, so `wt` is loaded here.)
                    "_withhold": wt.is_secret_read_tool_call(arguments),
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
            # Without a redactor nothing is readable; the read placeholder
            # would say this one result was recorded in a capture that is not.
            if target["_withhold"] and result is not None and redactor is not None:
                target["result"] = WITHHELD_READ_RESULT
            else:
                target["result"] = clip(scrub(result, RESULT_KIND), MAX_RESULT)
    if orphans:
        out["errors"].append(
            "session %s of %s: %d tool result(s) matched no call" % (sid, profile, orphans)
        )
    return count


def sessions_for(card, assignee, runs):
    if wt is None:
        # The mapping is the shared module's; without it no store is opened.
        unread("card %s: transcript rules unavailable; sessions not read" % card)
        return []
    stamped, _ = wt.stamped_worker_sessions(runs, assignee)
    found = [(sid, profile, "run_metadata") for sid, profile in stamped]
    dispatched = [r["profile"] or assignee for r in runs]
    profiles = list(dict.fromkeys(dispatched + [assignee]))
    for profile in profiles:
        if not profile:
            continue
        path = state_db(profile)
        if not os.path.exists(path):
            # A name no run was dispatched to has no store because nothing
            # ran as it: the observation, not a hole in it.
            note = unread if profile in dispatched else out["errors"].append
            note("no session store for profile %s" % profile)
            continue
        try:
            conn = ro(path)
            rows = conn.execute(
                "SELECT session_id, content FROM messages WHERE role = 'user' "
                "AND content LIKE ? ESCAPE '\\' ORDER BY id",
                ("%" + wt.WORKER_PROMPT_PREFIX + like_escape(card) + "%",),
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            unread("session store for %s: %s" % (profile, exc))
            continue
        # LIKE's trailing wildcard would also take a card whose id merely
        # starts with this one; the shared pattern ends the id where the prompt's does.
        exact = wt.worker_prompt_exact_re(card)
        for row in rows:
            sid = row["session_id"]
            if exact.search(row["content"] or "") and sid not in [f[0] for f in found]:
                found.append((sid, profile, "worker_prompt"))
    return found


try:
    kb = ro(ROOT + "/kanban.db")
except sqlite3.Error as exc:
    unread("kanban board: %s" % exc)
    kb = None

cards = {}
queue = list(roots)
while kb is not None and queue:
    tid = queue.pop(0)
    if tid in cards:
        continue
    if len(cards) >= MAX_CARDS:
        stopped_at("cards")
        break
    try:
        row = kb.execute(
            "SELECT id, assignee, status FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        if row is None:
            unread("card %s is not on the board" % tid)
            cards[tid] = None
            continue
        runs = kb.execute(
            "SELECT id, profile, status, started_at, ended_at, metadata, outcome, error "
            "FROM task_runs WHERE task_id = ? ORDER BY id",
            (tid,),
        ).fetchall()
        # Two kinds of edge, kept apart: ``fanned_out`` is the cards this
        # card's worker filed (the creator-of table the image adds), the
        # rest of ``children`` are predecessor links, which also gate cards
        # nobody here filed. The walk follows both; a lineage follows the
        # first alone.
        fanned_out = []
        if has_table(kb, "kanban_worker_children"):
            fanned_out = [
                r["child_id"]
                for r in kb.execute(
                    "SELECT child_id FROM kanban_worker_children WHERE creator_id = ? "
                    "ORDER BY created_at, child_id",
                    (tid,),
                )
            ]
        children = list(fanned_out) + [
            r["child_id"]
            for r in kb.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
                (tid,),
            )
        ]
    except sqlite3.Error as exc:
        unread("card %s: %s" % (tid, exc))
        cards[tid] = None
        continue
    card = {
        "task": tid,
        "assignee": row["assignee"],
        "status": row["status"],
        "runs": len(runs),
        # How each attempt ended, as the dispatcher recorded it: a worker out
        # of its turn budget closes its run ``timed_out`` with the budget in
        # ``error`` (``Iteration budget exhausted (N/M)``), which is the one
        # place the cap a worker actually ran under can be read back.
        "run_outcomes": [
            {"profile": r["profile"], "status": r["status"], "outcome": r["outcome"], "error": r["error"]}
            for r in runs
        ],
        "children": list(dict.fromkeys(children)),
        "fanned_out": list(dict.fromkeys(fanned_out)),
        "sessions": [],
    }
    cards[tid] = card
    queue.extend(card["children"])
    for sid, profile, match in sessions_for(tid, row["assignee"], runs):
        try:
            conn = ro(state_db(profile))
            calls = read_session(conn, tid, profile, sid)
        except sqlite3.Error as exc:
            unread("session %s of %s: %s" % (sid, profile, exc))
            continue
        # The calls are already in out["calls"]; a usage read that fails must
        # not cost the session its place in the map, only its counts.
        try:
            tokens = session_tokens(conn, profile, sid)
        except sqlite3.Error as exc:
            out["errors"].append("session %s of %s: token counts: %s" % (sid, profile, exc))
            tokens = None
        conn.close()
        card["sessions"].append(
            {"id": sid, "agent": profile, "match": match, "calls": calls, "tokens": tokens}
        )
    # Without the rules module `sessions_for` has already said no store was
    # opened; "no session found" would send a reader to the store.
    if not card["sessions"] and runs and wt is not None:
        unread("no session found for card %s" % tid)

out["cards"] = [c for c in cards.values() if c is not None]
for entry in out["calls"]:
    entry.pop("_id", None)
    entry.pop("_withhold", None)
print(SENTINEL)
reply = json.dumps(out, default=str) + "\n"
for start in range(0, len(reply), WRITE_CHUNK):
    sys.stdout.write(reply[start:start + WRITE_CHUNK])
    sys.stdout.flush()
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
        tokens: Each profile's token counts summed over the sessions it
            worked in, keyed by profile and then by hermes' column name
            (:data:`TOKEN_COLUMNS` in the pod). A session whose store had no
            counts is absent from the sums and named in ``summary["errors"]``.
            The harness maps these into its buckets and adds them to the run's
            totals (``harness._fold_worker_tokens``).
        unbilled: The sessions that reported no counts, as ``agent`` /
            ``task`` / ``session``, so a record whose sums are partial says
            so on disk (``summary`` does not reach ``results.json``).
    """

    entries: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    tokens: dict[str, dict[str, int]] = field(default_factory=dict)
    unbilled: list[dict[str, str]] = field(default_factory=list)


def gaps(summary: dict[str, Any] | None) -> list[str] | None:
    """What a capture could not read, from its ``summary``.

    ``None`` when the read did not run at all; otherwise every read the pod
    could not make (``unread``: a store, card or session it could not open,
    a dispatched card with no session), plus the ``CLIP_GAPS`` entry for
    each cap that clipped the read. The rest of ``errors`` is not a gap: a withheld
    redactor or an orphan result leaves the worker tags whole, and a name
    no run was dispatched to has no store because nothing ran as it. An
    empty list means the capture is complete, so a profile missing from it
    did not work the run.
    """
    if summary is None:
        return None
    problems = [str(e) for e in summary.get("unread") or []]
    problems += [CLIP_GAPS[c] for c in summary.get("clipped") or [] if c in CLIP_GAPS]
    return problems


def parents(summary: dict[str, Any] | None) -> dict[str, str] | None:
    """Child card to parent card, from the capture's card walk.

    ``None`` when the read did not run. A card the front agent filed has no
    entry; a card a worker fanned out (``kanban_worker_children``) maps to
    the card whose worker filed it. Predecessor links (``task_links``) are
    not lineage: a card gated on another was not necessarily filed by it.
    The ordered checks use the map to treat a card and the cards its worker
    spawned as one worker's work.
    """
    if not isinstance(summary, dict):
        return None
    found: dict[str, str] = {}
    for card in summary.get("cards") or []:
        # A summary that lists card ids rather than card records (the inject
        # transport's) carries no edges.
        if not isinstance(card, dict):
            continue
        for child in card.get("fanned_out") or []:
            found.setdefault(str(child), str(card.get("task") or ""))
    return found


def runs(summary: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Every attempt of every captured card, in board order, with its card id.

    ``None`` when the read did not run. Each row is the card's ``run_outcomes``
    entry (``profile``, ``status``, ``outcome``, ``error``) plus ``task``; a
    summary without card records yields an empty list. ``worker_budget`` reads the rows for the
    turn budget a worker ran out of.
    """
    if not isinstance(summary, dict):
        return None
    rows: list[dict[str, Any]] = []
    for card in summary.get("cards") or []:
        if not isinstance(card, dict):
            continue
        for run in card.get("run_outcomes") or []:
            if isinstance(run, dict):
                rows.append({"task": str(card.get("task") or ""), **run})
    return rows



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
            SUPPLEMENT_PATH,
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
        "unread": [str(e) for e in payload.get("unread") or []],
        "truncated": bool(payload.get("truncated")),
        "clipped": [str(c) for c in payload.get("clipped") or []],
        "calls": len(entries),
    }
    for problem in summary["errors"]:
        _log.warning("worker trajectory: %s", problem)
    by_agent, unbilled = _tokens_by_agent(summary["cards"])
    return WorkerCapture(entries=entries, summary=summary, tokens=by_agent, unbilled=unbilled)


def _tokens_by_agent(
    cards: list[Any],
) -> tuple[dict[str, dict[str, int]], list[dict[str, str]]]:
    """Sum each session's counts into its profile's, in hermes' column names.

    A retried card ran in several sessions and a profile works several cards
    in one run; both add up here. A session that reported no counts (``None``,
    named in the pod's errors) contributes nothing rather than zero, and is
    returned in the second element so the record can say the sums are partial.
    """
    by_agent: dict[str, dict[str, int]] = {}
    unbilled: list[dict[str, str]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        for session in card.get("sessions") or []:
            if not isinstance(session, dict):
                continue
            agent = str(session.get("agent") or "")
            if not isinstance(session.get("tokens"), dict):
                unbilled.append(
                    {
                        "agent": agent,
                        "task": str(card.get("task") or ""),
                        "session": str(session.get("id") or ""),
                    }
                )
                continue
            counts = by_agent.setdefault(agent, {})
            for column, value in session["tokens"].items():
                if isinstance(value, int) and not isinstance(value, bool):
                    counts[column] = counts.get(column, 0) + value
    return by_agent, unbilled
