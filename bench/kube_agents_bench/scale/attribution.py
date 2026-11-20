"""Model tokens by capability, from the install's session records: the first step of
docs/designs/capability-usage-attribution.md ("What is built, in order", item 1).

Hermes keeps each session's token counts on its row in the profile's ``state.db``.
A cron session's id names its job (``cron_<job>_<date>_<time>``); a kanban worker's
session is joined to its card through the card's run, and the card to the session
that created it; an event watcher's session is named ``k8s-evt-<id>``. Each session
is joined to one capability after the run, and the sum is checked against LiteLLM's
counters for the same window. No runtime change.

The join is a pure function over what the gateway pod's stores hold, so it is
tested on its own; :func:`read_stores` does the reading.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kube_agents_bench.scale import kube, operations

REPO = Path(__file__).resolve().parents[3]
ROSTERS = (REPO / "agents" / "platform" / "cron" / "jobs.json",
           REPO / "agents" / "chat" / "defaults" / "cron" / "jobs.json")
DATA_ROOT = "/opt/data"
READ_TIMEOUT_SECONDS = 120
# kubectl exec into the sandboxed pod cuts stdout at 64 KiB, so a whole history is fetched in
# pieces well under that, from a file the read script writes in the pod's temp directory.
CHUNK_CHARS = 48 * 1024
POD_OUTPUT_DIR = "/tmp"
POD_OUTPUT_PREFIX = "kage-usage-"
REMOVE = "remove"
# The read takes sessions and cards from this long before the run's start, so a parent or
# creator session that started earlier is still there to join through.
READ_SLACK_SECONDS = 6 * 3600

# The rows beyond the two cron rosters (the design's "The capability list").
FIRST_RUN = "first-run-flow"
INCIDENT_TRIAGE = "incident-triage"
CHAT = "chat"
MEMORY_SERVICE = "memory-service"
SESSION_PODS = "session-pods"
GITOPS_DRIFT = "gitops-drift"
EXTRA_ROWS = (FIRST_RUN, INCIDENT_TRIAGE, CHAT, MEMORY_SERVICE, SESSION_PODS, GITOPS_DRIFT)
UNATTRIBUTED = "unattributed"
PLATFORM_BASE = "platform-base"
OTHER = "other"
OOBE_JOB = "oobe"
TOP_ROWS = 5
# The design's two checks: the rows add up to LiteLLM's counters, and Unattributed stays small.
SUM_TOLERANCE = 0.10
UNATTRIBUTED_LIMIT = 0.05

CRON_ID = re.compile(r"^cron_(?P<job>.+)_\d{8}_\d{6}$")
EVENT_ID = re.compile(r"^k8s-evt-")
# A card's origin, stamped by the kanban patch, when its creator session is unknown.
ORIGINS = {"event": INCIDENT_TRIAGE, "chat": CHAT}
# Chat sessions: the agent API and the chat gateways.
CHAT_SOURCES = {"api_server", "cli", "telegram", "slack", "discord", "google_chat", "webhook"}
TOKEN_FIELDS = ("input", "cached", "output", "reasoning")
# An inject's session, by the reason the Session KV server records on its intercepted_events
# row (agents/platform/scripts/session_kv_server.py, STALL_LEDGER_REASON and
# DRIFT_LEDGER_REASON), counts under the capability that raised it: the design's "An inject
# counts under the capability that raised it". An event's own reasons stay incident triage.
INJECT_REASONS = {"ControllerStall": "stall-watch", "OutOfBandChange": GITOPS_DRIFT}
SESSION_KV_DB = "/var/lib/kube-agents/session/session_kv.db"
KANBAN_SOURCE = "kanban"
# Profile kinds for each row's split: the front door, the Platform Agent, the Cluster Agents.
FRONT_DOOR_PROFILE = "default"
PLATFORM_PROFILE = "platform"
CLUSTER_PROFILE_PREFIX = "cluster-"
CLUSTER_AGENTS = "cluster-agents"
# The first-run flow's cards carry these idempotency keys (agents/chat/scripts/bootstrap_scan_gate.py).
FIRST_RUN_KEY_PREFIX = "bootstrap-"
# A run that ended blocked or failed keeps no worker session id; its worker session starts
# within this many seconds after the run does.
RUN_START_SLACK_SECONDS = 60

# Runs in the gateway pod under hermes' venv: every profile's sessions and the kanban
# board since argv[2], as one JSON object, zlib-compressed and base64-encoded on stdout.
# kubectl exec into the sandboxed pod cuts stdout at 64 KiB, and a busy install's records
# pass that; compressed, they are about a tenth. Read only.
READ_SCRIPT = r"""
import base64, glob, json, os, sqlite3, sys, zlib
root, since = sys.argv[1], float(sys.argv[2])
def ro(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn
out = {"sessions": [], "cards": [], "runs": [], "errors": []}
stores = [("default", os.path.join(root, "state.db"))]
stores += [(p.split("/")[-2], p) for p in sorted(glob.glob(os.path.join(root, "profiles", "*", "state.db")))]
for profile, path in stores:
    try:
        conn = ro(path)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(sessions)")}
        ends = [c for c in ("ended_at", "last_activity_at") if c in cols]
        end = "COALESCE(%s) AS ended_at" % ", ".join(ends) if ends else "NULL AS ended_at"
        for r in conn.execute(
                "SELECT id, source, started_at, %s, parent_session_id, input_tokens, output_tokens, "
                "cache_read_tokens, reasoning_tokens FROM sessions WHERE started_at >= ?" % end, (since,)):
            out["sessions"].append(dict(r, profile=profile))
        mcols = {c[1] for c in conn.execute("PRAGMA table_info(messages)")}
        if {"session_id", "timestamp"} <= mcols:
            tok = "COALESCE(SUM(token_count), 0)" if "token_count" in mcols else "0"
            for r in conn.execute(
                    "SELECT session_id, CAST(timestamp / 60 AS INTEGER) AS minute, COUNT(*) AS messages, %s AS tokens "
                    "FROM messages WHERE timestamp >= ? GROUP BY session_id, minute" % tok, (since,)):
                out.setdefault("activity", []).append(list(r))
    except sqlite3.Error as exc:
        out["errors"].append("%s: %s" % (path, exc))
out["injects"] = []
kv = os.environ.get("SESSION_KV_DB_PATH") or (sys.argv[3] if len(sys.argv) > 3 else "")
if kv and sys.argv[4:] and os.path.exists(kv):
    try:
        conn = ro(kv)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='intercepted_events'").fetchone():
            for r in conn.execute("SELECT session_id, object_uid, reason FROM intercepted_events WHERE reason IN (%s)"
                                  % ",".join("?" for _ in sys.argv[4:]), sys.argv[4:]):
                out["injects"].append(dict(r))
    except sqlite3.Error as exc:
        out["errors"].append("%s: %s" % (kv, exc))
# Every model call, from each profile's span store: a span named api.<model> carries the call's
# exact usage and its session, and is written whether or not the session reaches state.db.
# Aggregated here by profile, session and minute, so the output stays small.
calls = {}
for profile, path in stores:
    span_db = os.path.join(os.path.dirname(path), "hermes_otel_live.db")
    if not os.path.exists(span_db):
        continue
    try:
        for (data,) in ro(span_db).execute(
                "SELECT data FROM events WHERE kind = 'span' AND data LIKE '%\"name\": \"api.%'"):
            d = json.loads(data)
            if not d.get("name", "").startswith("api."):
                continue
            a = d.get("attributes", {})
            ts = d.get("start_time_unix_nano", 0) / 1e9
            if ts < since:
                continue
            key = (profile, a.get("session.id") or a.get("session_id") or "", int(ts // 60))
            row = calls.setdefault(key, [0, 0, 0, 0])
            row[0] += 1
            row[1] += int(a.get("gen_ai.usage.input_tokens") or 0)
            row[2] += int(a.get("gen_ai.usage.cache_read_input_tokens") or 0)
            row[3] += int(a.get("gen_ai.usage.output_tokens") or 0)
    except (sqlite3.Error, ValueError) as exc:
        out["errors"].append("%s: %s" % (span_db, exc))
out["calls"] = [list(k) + v for k, v in calls.items()]
board = os.path.join(root, "kanban.db")
if os.path.exists(board):
    try:
        conn = ro(board)
        has_origins = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_task_origins'").fetchone()
        join = " LEFT JOIN kanban_task_origins o ON o.task_id = t.id" if has_origins else ""
        origin = "o.origin" if has_origins else "NULL"
        for r in conn.execute("SELECT t.id, t.title, t.body, t.session_id, t.idempotency_key, %s AS origin "
                              "FROM tasks t%s WHERE t.created_at >= ?" % (origin, join), (since,)):
            out["cards"].append(dict(r))
        for r in conn.execute("SELECT task_id, profile, started_at, metadata FROM task_runs WHERE started_at >= ?",
                              (since,)):
            out["runs"].append(dict(r))
    except sqlite3.Error as exc:
        out["errors"].append("%s: %s" % (board, exc))
blob = base64.b64encode(zlib.compress(json.dumps(out).encode())).decode()
dest = os.environ.get("KAGE_USAGE_OUT")
if dest:
    with open(dest, "w") as f:
        f.write(blob)
    print(len(blob))
else:
    print(blob)
"""

# Prints `length` characters of the file at argv[1] from offset argv[2]; with argv[3] == "remove",
# removes the file instead. Read only, apart from the read script's own output file.
CHUNK_SCRIPT = r"""
import os, sys
path = sys.argv[1]
if sys.argv[2] == "remove":
    os.remove(path)
else:
    with open(path) as f:
        f.seek(int(sys.argv[2]))
        sys.stdout.write(f.read(int(sys.argv[3])))
"""


def capabilities() -> tuple[str, ...]:
    """Every capability row: each job in the two cron rosters by its id, then the fixed rows."""
    jobs = []
    for roster in ROSTERS:
        jobs += [job["id"] for job in json.loads(roster.read_text())["jobs"]]
    return tuple(jobs) + EXTRA_ROWS


def read_stores(context: str, namespace: str, since: float = 0.0) -> dict[str, Any]:
    """The sessions, cards and card runs the gateway pod's stores hold, from ``since`` less the slack."""
    pod = operations._gateway_pod(context, namespace)
    if not pod:
        raise kube.CommandError("no gateway pod to read the session records in")
    dest = f"{POD_OUTPUT_DIR}/{POD_OUTPUT_PREFIX}{uuid.uuid4().hex}"

    def exec_python(*argv: str) -> str:
        return kube.kubectl(context, "exec", "-n", namespace, pod, "-c", operations.AGENT_CONTAINER, "--",
                            "env", f"KAGE_USAGE_OUT={dest}", operations.JOB_PYTHON, "-c", *argv,
                            timeout=READ_TIMEOUT_SECONDS)

    length = int(exec_python(READ_SCRIPT, DATA_ROOT, str(max(0.0, since - READ_SLACK_SECONDS)), SESSION_KV_DB,
                             *INJECT_REASONS).strip())
    try:
        blob = "".join(exec_python(CHUNK_SCRIPT, dest, str(offset), str(CHUNK_CHARS))
                       for offset in range(0, length, CHUNK_CHARS))
    finally:
        exec_python(CHUNK_SCRIPT, dest, REMOVE)
    if len(blob) != length:
        raise kube.CommandError(f"the session records' read came back {len(blob)} of {length} characters")
    return decode(blob)


def archive(stores: dict[str, Any], path: Path) -> None:
    """Keep the stores as they were, gzipped JSON, so any period in them can be measured again later."""
    path.write_bytes(gzip.compress(json.dumps(stores).encode()))


def load_archive(path: Path) -> dict[str, Any]:
    return json.loads(gzip.decompress(path.read_bytes()))


def decode(out: str) -> dict[str, Any]:
    """The read script's output; a cut-off read is an error that says so, not a JSON parse failure."""
    try:
        return json.loads(zlib.decompress(base64.b64decode(out.strip())))
    except (ValueError, zlib.error) as exc:
        raise kube.CommandError(f"the session records' read came back incomplete ({len(out)} characters): {exc}") \
            from exc


def tokens(session: dict[str, Any]) -> dict[str, int]:
    """A session's tokens; ``input`` excludes the cached input, as Hermes stores it."""
    return {"input": int(session.get("input_tokens") or 0), "cached": int(session.get("cache_read_tokens") or 0),
            "output": int(session.get("output_tokens") or 0),
            "reasoning": int(session.get("reasoning_tokens") or 0)}


def profile_kind(profile: str | None) -> str:
    """The row split's kind: the front door, the Platform Agent, the Cluster Agents, or the profile itself."""
    if profile and profile.startswith(CLUSTER_PROFILE_PREFIX):
        return CLUSTER_AGENTS
    return profile or FRONT_DOOR_PROFILE


def mentioned_job(text: str, jobs: tuple[str, ...]) -> str | None:
    """The longest job id a card's text names, so an audit run from a card counts under the audit."""
    named = [job for job in jobs if re.search(rf"(?<![\w-]){re.escape(job)}(?![\w-])", text)]
    return max(named, key=len) if named else None


@dataclass
class Join:
    capability: dict[str, str]  # session id -> capability
    rules: dict[str, str]  # session id -> the rule that decided it


def join(stores: dict[str, Any], caps: tuple[str, ...]) -> Join:
    """Each session's capability, by the design's rules; a session no rule reaches is Unattributed."""
    jobs = tuple(c for c in caps if c not in EXTRA_ROWS)
    sessions = {s["id"]: s for s in stores.get("sessions", [])}
    cards = {c["id"]: c for c in stores.get("cards", [])}
    worker_card: dict[str, str] = {}
    unnamed_runs = []
    for run in stores.get("runs", []):
        try:
            worker = json.loads(run.get("metadata") or "{}").get("worker_session_id")
        except (ValueError, AttributeError):
            worker = None
        if worker:
            worker_card[worker] = run["task_id"]
        else:
            unnamed_runs.append(run)
    for sid, session in sessions.items():
        if session.get("source") != "kanban" or sid in worker_card:
            continue
        started = float(session.get("started_at") or 0)
        near = [r for r in unnamed_runs if r.get("profile") in (None, session.get("profile"))
                and 0 <= started - float(r.get("started_at") or 0) <= RUN_START_SLACK_SECONDS]
        if near:
            worker_card[sid] = max(near, key=lambda r: float(r.get("started_at") or 0))["task_id"]
    # The stall watch keys its row by the session it opens (object_uid); the drift path and the
    # event path by session_id. Either names the inject's session.
    injected: dict[str, str] = {}
    for row in stores.get("injects", []):
        for key in (row.get("session_id"), row.get("object_uid")):
            if key and row.get("reason") in INJECT_REASONS:
                injected[key] = INJECT_REASONS[row["reason"]]
    result = Join({}, {})
    # Under spec.mode next a delegation travels the agent bus, not the board: its worker's kanban
    # session has no card. It joins to the chat turn open when it started, when exactly one was.
    chats = [s for s in sessions.values() if s.get("source") in CHAT_SOURCES and not EVENT_ID.match(s["id"])]

    def open_chat(session: dict[str, Any]) -> str | None:
        started = float(session.get("started_at") or 0)
        live = [c["id"] for c in chats if float(c.get("started_at") or 0) <= started
                <= float(c.get("ended_at") or c.get("started_at") or 0)]
        return live[0] if len(live) == 1 else None

    def decide(sid: str, seen: frozenset = frozenset()) -> tuple[str, str]:
        if sid in result.capability:
            return result.capability[sid], result.rules[sid]
        session = sessions.get(sid, {})
        cron = CRON_ID.match(sid)
        if cron:
            job = cron.group("job")
            if job == OOBE_JOB:
                return FIRST_RUN, "cron:oobe"
            return (job, "cron") if job in caps else (UNATTRIBUTED, f"cron job {job} not in the rosters")
        if sid in worker_card:
            card = cards.get(worker_card[sid], {})
            if (card.get("idempotency_key") or "").startswith(FIRST_RUN_KEY_PREFIX):
                return FIRST_RUN, "first-run card"
            job = mentioned_job(f"{card.get('title') or ''}\n{card.get('body') or ''}", jobs)
            if job:
                return job, "card names the job"
            creator = card.get("session_id")
            if creator and creator not in seen and creator != sid:
                cap = decide(creator, seen | {sid})[0]
                if cap != UNATTRIBUTED:
                    return cap, "card creator"
            # A creator with no row (pruned) or one no rule reaches: the card's own origin decides.
            if card.get("origin") in ORIGINS:
                return ORIGINS[card["origin"]], "card origin"
            return UNATTRIBUTED, "card with no creator or origin"
        if sid in injected:
            cap = injected[sid]
            return (cap, "inject") if cap in caps else (UNATTRIBUTED, f"inject from {cap}, not in the list")
        if EVENT_ID.match(sid):
            return INCIDENT_TRIAGE, "event session"
        parent = session.get("parent_session_id")
        if parent and parent not in seen and parent != sid:
            cap = decide(parent, seen | {sid})[0]
            if cap != UNATTRIBUTED:
                return cap, "parent session"
        if session.get("source") in CHAT_SOURCES:
            return CHAT, f"source {session.get('source')}"
        if session.get("source") == KANBAN_SOURCE:
            chat = open_chat(session)
            if chat and chat not in seen:
                return decide(chat, seen | {sid})[0], "bus delegation from the one open chat turn"
            return UNATTRIBUTED, "kanban session with no card and not one open chat turn"
        return UNATTRIBUTED, f"source {session.get('source')}"

    called = {c[1] for c in stores.get("calls", []) if c[1]}
    for sid in list(sessions) + sorted(called - set(sessions)):
        cap, rule = decide(sid)
        result.capability[sid], result.rules[sid] = cap, rule
    return result


def call_sessions(stores: dict[str, Any]) -> set[str]:
    """The sessions whose span calls stand in for their state.db row.

    The span store is a rolling buffer, so a read after a long run has lost the oldest calls. A
    session's calls stand in only when they hold at least its row's tokens, or when it has no row
    because it never reached state.db; every other session keeps its row.
    """
    held: dict[str, int] = {}
    for _profile, sid, _minute, _n, input_all, _cached, output in stores.get("calls") or []:
        if sid:
            held[sid] = held.get(sid, 0) + input_all + output
    rows = {s["id"]: s for s in stores.get("sessions", [])}
    return {sid for sid, total in held.items()
            if sid not in rows or total >= sum(v for k, v in tokens(rows[sid]).items() if k != "reasoning")}


def attribute(stores: dict[str, Any], start: float, end: float, litellm: dict[str, float] | None = None,
              caps: tuple[str, ...] | None = None) -> dict[str, Any]:
    """One window's tokens by capability, the overview rows, and the two checks.

    A session whose calls the span store (``stores["calls"]``) still holds counts call by call, in
    the minute each was made; any other session counts in the window it started in, from its
    state.db row (``call_sessions``). ``litellm`` is the window's counter growth
    (``tokens_input`` including cached input, ``tokens_cached``, ``tokens_output``), as the sized
    run's measurements record it.
    """
    caps = caps or capabilities()
    joined = join(stores, caps)
    rows: dict[str, dict[str, int]] = {}
    unattributed_rules: dict[str, int] = {}
    by_calls = call_sessions(stores)
    used: set[str] = set()
    if stores.get("calls"):
        # Each model call counts in the minute it was made, from the span store's exact usage.
        seen: dict[str, set] = {}
        for profile, sid, minute, n, input_all, cached, output in stores["calls"]:
            if not start <= minute * 60 < end or (sid and sid not in by_calls):
                continue
            used.add("calls")
            cap = joined.capability.get(sid, UNATTRIBUTED) if sid else UNATTRIBUTED
            row = rows.setdefault(cap, {**dict.fromkeys(TOKEN_FIELDS, 0), "sessions": 0, "by_profile": {},
                                        "calls": 0})
            row["input"] += max(0, input_all - cached)
            row["cached"] += cached
            row["output"] += output
            row["calls"] = row.get("calls", 0) + n
            kind = profile_kind(profile)
            row["by_profile"][kind] = row["by_profile"].get(kind, 0) + input_all + output
            if sid not in seen.setdefault(cap, set()):
                seen[cap].add(sid)
                row["sessions"] += 1
                if cap == UNATTRIBUTED:
                    rule = joined.rules.get(sid, "call with no session id") if sid else "call with no session id"
                    unattributed_rules[rule] = unattributed_rules.get(rule, 0) + 1
    for session in stores.get("sessions", []):
        started = float(session.get("started_at") or 0)
        if session["id"] in by_calls or not start <= started < end:
            continue
        used.add("sessions")
        cap = joined.capability[session["id"]]
        row = rows.setdefault(cap, {**dict.fromkeys(TOKEN_FIELDS, 0), "sessions": 0, "by_profile": {}})
        kind = profile_kind(session.get("profile"))
        split = row["by_profile"].setdefault(kind, 0)
        for name, value in tokens(session).items():
            row[name] += value
        row["by_profile"][kind] = split + sum(v for k, v in tokens(session).items() if k != "reasoning")
        row["sessions"] += 1
        if cap == UNATTRIBUTED:
            rule = joined.rules[session["id"]]
            unattributed_rules[rule] = unattributed_rules.get(rule, 0) + 1
    session_total = sum(r["input"] + r["cached"] + r["output"] for r in rows.values())
    # What the install's own records hold, before any LiteLLM-only tokens are added below.
    recorded = {"input": sum(r["input"] + r["cached"] for r in rows.values()),
                "output": sum(r["output"] for r in rows.values()),
                "source": " and ".join(sorted(used)) or "sessions"}
    sum_check = sum_against_litellm(rows, litellm)
    # Model calls made outside any Hermes session (the memory service's, for one) appear in
    # LiteLLM's counters only; the design counts them in Unattributed.
    outside = outside_sessions(rows, litellm)
    if any(outside.values()):
        row = rows.setdefault(UNATTRIBUTED, {**dict.fromkeys(TOKEN_FIELDS, 0), "sessions": 0, "by_profile": {}})
        for name, value in outside.items():
            row[name] += value
        unattributed_rules["outside any session"] = 0
    total = sum(r["input"] + r["cached"] + r["output"] for r in rows.values())
    return {"capabilities": rows, "overview": overview(rows, total), "total_tokens": total,
            "session_tokens": session_total, "recorded": recorded, "outside_sessions": outside,
            "unattributed_reasons": unattributed_rules,
            "checks": {"unattributed": unattributed_check(rows, total), "sum": sum_check},
            "errors": list(stores.get("errors", []))}


def outside_sessions(rows: dict[str, dict[str, int]], litellm: dict[str, float] | None) -> dict[str, int]:
    """LiteLLM's tokens beyond the session rows' sums: input (cached apart) and output."""
    if not litellm:
        return {"input": 0, "cached": 0, "output": 0}
    cached = max(0, int(litellm.get("tokens_cached", 0)) - sum(r["cached"] for r in rows.values()))
    uncached = int(litellm.get("tokens_input", 0)) - int(litellm.get("tokens_cached", 0))
    return {"input": max(0, uncached - sum(r["input"] for r in rows.values())), "cached": cached,
            "output": max(0, int(litellm.get("tokens_output", 0)) - sum(r["output"] for r in rows.values()))}


def overview(rows: dict[str, dict[str, int]], total: int) -> list[dict[str, Any]]:
    """The five largest capabilities, Other and Unattributed, adding up to the total."""
    def size(row: dict[str, int]) -> int:
        return row["input"] + row["cached"] + row["output"]
    named = sorted(((c, size(r)) for c, r in rows.items() if c != UNATTRIBUTED), key=lambda x: -x[1])
    lines = [{"capability": c, "tokens": n} for c, n in named[:TOP_ROWS]]
    lines.append({"capability": OTHER, "tokens": sum(n for _, n in named[TOP_ROWS:])})
    lines.append({"capability": UNATTRIBUTED, "tokens": size(rows[UNATTRIBUTED]) if UNATTRIBUTED in rows else 0})
    for line in lines:
        line["share"] = round(line["tokens"] / total, 4) if total else 0.0
    return lines


def unattributed_check(rows: dict[str, dict[str, int]], total: int) -> dict[str, Any]:
    """The Unattributed share of the window's tokens, outside-session tokens included, under 5 %."""
    unattributed = rows.get(UNATTRIBUTED, {})
    share = (unattributed.get("input", 0) + unattributed.get("cached", 0) + unattributed.get("output", 0)) / total \
        if total else 0.0
    return {"share": round(share, 4), "passed": share < UNATTRIBUTED_LIMIT}


def sum_against_litellm(rows: dict[str, dict[str, int]], litellm: dict[str, float] | None) -> dict[str, Any]:
    """The session rows against LiteLLM's counters, input and output apart, cached input counted once."""
    if litellm is None:
        return {"passed": False, "reason": "no LiteLLM counters for the window"}
    ours = {"input": sum(r["input"] + r["cached"] for r in rows.values()),
            "output": sum(r["output"] for r in rows.values())}
    theirs = {"input": float(litellm.get("tokens_input", 0)), "output": float(litellm.get("tokens_output", 0))}
    # No traffic on either side is a sound window; traffic on one side only is not.
    ratios = {k: round(ours[k] / theirs[k], 4) if theirs[k] else (1.0 if not ours[k] else None) for k in ours}
    passed = all(r is not None and abs(r - 1) <= SUM_TOLERANCE for r in ratios.values())
    return {"sessions": ours, "litellm": theirs, "ratio": ratios, "passed": passed}


def markdown(record: dict[str, dict[str, Any]]) -> str:
    """The token overview and a row for every capability, for each window in ``record``."""
    lines = ["# Model tokens by capability", ""]
    for window, body in record.items():
        if "error" in body:
            lines += [f"## {window} window (not read)", "", body["error"], ""]
            continue
        sound = all(c.get("passed") for c in body["checks"].values())
        lines += [f"## {window} window ({'sound' if sound else 'unsound'})", "",
                  "| Capability | Tokens | Share |", "| --- | ---: | ---: |"]
        lines += [f"| {r['capability']} | {r['tokens']:,} | {r['share']:.1%} |" for r in body["overview"]]
        lines += ["", "| Capability | Sessions | Input | Cached input | Output | By profile |",
                  "| --- | ---: | ---: | ---: | ---: | --- |"]
        for cap, row in sorted(body["capabilities"].items()):
            split = ", ".join(f"{k} {v:,}" for k, v in sorted(row.get("by_profile", {}).items(), key=lambda kv: -kv[1]))
            lines.append(f"| {cap} | {row['sessions']} | {row['input']:,} | {row['cached']:,} | {row['output']:,} "
                         f"| {split} |")
        if body.get("memory", {}).get("rows"):
            lines += ["", "| Capability | Memory-time (GiB-hours) | Share | Peak share (GiB) |", "| --- | ---: | ---: | ---: |"]
            lines += [f"| {r['capability']} | {r['gib_hours']:.3f} | {r['share']:.1%} | {r['peak_gib'] if r['peak_gib'] is not None else ''} |"
                      for r in body["memory"]["rows"]]
        lines += ["", f"Checks: `{json.dumps(body['checks'])}`", ""]
    return "\n".join(lines)


GIB = 2 ** 30
SECONDS_PER_HOUR_F = 3600.0


def memory_by_capability(series: dict[str, list[tuple[float, float]]], stores: dict[str, Any], start: float,
                         end: float, base: dict[str, float] | None = None,
                         caps: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Each capability's share of the containers' memory-time over [start, end): the design's
    shared-process estimate (docs/designs/capability-usage-attribution.md, "Memory").

    ``series`` is each container's memory in bytes per sample, ``(time, bytes)``. A container's
    memory above its base is split, sample by sample, among the sessions active in that minute,
    weighted by their model calls in it, or their messages for a session whose calls the span
    store no longer holds (``call_sessions``); live sessions with no activity in the minute share it
    equally; with no live session it is Unattributed. ``base`` is each container's idle memory,
    held fixed for the period; by default its first sample in the period.
    """
    caps = caps or capabilities()
    joined = join(stores, caps)
    sessions = {s["id"]: s for s in stores.get("sessions", [])}
    by_calls = call_sessions(stores)
    activity: dict[int, dict[str, float]] = {}
    # A minute's excess goes to the sessions active in it: by their model calls where the span store
    # still holds them (call_sessions), by their messages otherwise.
    for _profile, sid, minute, n, *_usage in stores.get("calls") or []:
        if sid in by_calls:
            activity.setdefault(int(minute), {})[sid] = activity.get(int(minute), {}).get(sid, 0.0) + float(n)
    for sid, minute, messages, _tokens in stores.get("activity", []):
        if sid not in by_calls:
            activity.setdefault(int(minute), {})[sid] = activity.get(int(minute), {}).get(sid, 0.0) + float(messages)
    sessions = {**{sid: {"id": sid} for sid in by_calls}, **sessions}

    def live(t: float) -> list[str]:
        return [sid for sid, s in sessions.items()
                if float(s.get("started_at") or 0) <= t <= float(s.get("ended_at") or s.get("started_at") or 0)]

    gib_hours: dict[str, float] = {}
    base_gib_hours, peaks = 0.0, {}
    for container, points in series.items():
        inside = sorted((t, b) for t, b in points if start <= t < end)
        if not inside:
            continue
        idle = (base or {}).get(container, inside[0][1])
        for i, (t, used) in enumerate(inside):
            step = (inside[i + 1][0] - t) if i + 1 < len(inside) else (end - t)
            step = max(0.0, min(step, end - t)) / SECONDS_PER_HOUR_F
            base_gib_hours += min(used, idle) / GIB * step
            excess = max(0.0, used - idle) / GIB * step
            if not excess:
                continue
            weights = {sid: w for sid, w in activity.get(int(t // 60), {}).items() if sid in sessions}
            if not weights:
                weights = {sid: 1.0 for sid in live(t)}
            if not weights:
                gib_hours[UNATTRIBUTED] = gib_hours.get(UNATTRIBUTED, 0.0) + excess
                continue
            total = sum(weights.values())
            for sid, w in weights.items():
                cap = joined.capability.get(sid, UNATTRIBUTED)
                gib_hours[cap] = gib_hours.get(cap, 0.0) + excess * w / total
                share = excess * w / total / step if step else 0.0
                peaks[cap] = max(peaks.get(cap, 0.0), share)
    total = base_gib_hours + sum(gib_hours.values())
    rows = [{"capability": c, "gib_hours": round(v, 4), "share": round(v / total, 4) if total else 0.0,
             "peak_gib": round(peaks.get(c, 0.0), 3)} for c, v in sorted(gib_hours.items(), key=lambda kv: -kv[1])]
    rows.append({"capability": PLATFORM_BASE, "gib_hours": round(base_gib_hours, 4),
                 "share": round(base_gib_hours / total, 4) if total else 0.0, "peak_gib": None})
    return {"rows": rows, "total_gib_hours": round(total, 4),
            "method": "excess over each container's base, split each minute by the sessions' model calls or messages in it"}


def container_memory(install: Any, start: float, end: float) -> dict[str, list[tuple[float, float]]]:
    """Each install container's memory per minute over [start, end], its replicas summed, from Cloud Monitoring."""
    from kube_agents_bench.scale import collect  # the Monitoring reader; imported here to keep the join importable alone
    query = (f'metric.type="{collect.MEMORY_METRIC}" AND metric.labels.memory_type="{collect.MEMORY_TYPE}" '
             f'AND resource.type="k8s_container" AND resource.labels.project_id="{install.project}" '
             f'AND resource.labels.cluster_name="{install.cluster}" '
             f'AND resource.labels.namespace_name="{install.namespace}"')
    sums: dict[str, dict[float, float]] = {}
    for s in install._time_series(query, "ALIGN_MAX", start, end):
        container = s["resource"]["labels"].get("container_name", "")
        for p in s.get("points", []):
            t = datetime_ts(p["interval"]["endTime"])
            sums.setdefault(container, {})[t] = sums.get(container, {}).get(t, 0.0) + collect._point_value(p)
    return {c: sorted(v.items()) for c, v in sums.items() if c}


def datetime_ts(text: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
