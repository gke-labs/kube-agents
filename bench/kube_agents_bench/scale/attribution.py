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
import json
import re
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
# The first-run flow's cards carry these idempotency keys (agents/chat/scripts/oobe.py's scan).
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
print(base64.b64encode(zlib.compress(json.dumps(out).encode())).decode())
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
    out = kube.kubectl(context, "exec", "-n", namespace, pod, "-c", operations.AGENT_CONTAINER, "--",
                       operations.JOB_PYTHON, "-c", READ_SCRIPT, DATA_ROOT, str(max(0.0, since - READ_SLACK_SECONDS)),
                       SESSION_KV_DB, *INJECT_REASONS, timeout=READ_TIMEOUT_SECONDS)
    return decode(out)


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

    for sid in sessions:
        cap, rule = decide(sid)
        result.capability[sid], result.rules[sid] = cap, rule
    return result


def attribute(stores: dict[str, Any], start: float, end: float, litellm: dict[str, float] | None = None,
              caps: tuple[str, ...] | None = None) -> dict[str, Any]:
    """One window's tokens by capability, the overview rows, and the two checks.

    A session counts in the window it started in. ``litellm`` is the window's
    counter growth (``tokens_input`` including cached input, ``tokens_cached``,
    ``tokens_output``), as the sized run's measurements record it.
    """
    caps = caps or capabilities()
    joined = join(stores, caps)
    rows: dict[str, dict[str, int]] = {}
    unattributed_rules: dict[str, int] = {}
    for session in stores.get("sessions", []):
        started = float(session.get("started_at") or 0)
        if not start <= started < end:
            continue
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
            "session_tokens": session_total, "outside_sessions": outside,
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
        lines += ["", f"Checks: `{json.dumps(body['checks'])}`", ""]
    return "\n".join(lines)
