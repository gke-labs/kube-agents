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

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kube_agents_bench.scale import kube, operations

REPO = Path(__file__).resolve().parents[3]
ROSTERS = (REPO / "agents" / "platform" / "cron" / "jobs.json",
           REPO / "agents" / "chat" / "defaults" / "cron" / "jobs.json")
DATA_ROOT = "/opt/data"
READ_TIMEOUT_SECONDS = 120

# The rows beyond the two cron rosters (the design's "The capability list").
FIRST_RUN = "first-run-flow"
INCIDENT_TRIAGE = "incident-triage"
CHAT = "chat"
MEMORY_SERVICE = "memory-service"
SESSION_PODS = "session-pods"
EXTRA_ROWS = (FIRST_RUN, INCIDENT_TRIAGE, CHAT, MEMORY_SERVICE, SESSION_PODS)
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
# The first-run flow's cards carry these idempotency keys (agents/chat/scripts/oobe.py's scan).
FIRST_RUN_KEY_PREFIX = "bootstrap-"
# A run that ended blocked or failed keeps no worker session id; its worker session starts
# within this many seconds after the run does.
RUN_START_SLACK_SECONDS = 60

# Runs in the gateway pod under hermes' venv: every profile's sessions and the kanban
# board, as one JSON object on stdout. Read only.
READ_SCRIPT = r"""
import glob, json, os, sqlite3, sys
root = sys.argv[1]
def ro(path):
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn
out = {"sessions": [], "cards": [], "runs": [], "errors": []}
stores = [("default", os.path.join(root, "state.db"))]
stores += [(p.split("/")[-2], p) for p in sorted(glob.glob(os.path.join(root, "profiles", "*", "state.db")))]
for profile, path in stores:
    try:
        for r in ro(path).execute(
                "SELECT id, source, started_at, parent_session_id, input_tokens, output_tokens, "
                "cache_read_tokens, reasoning_tokens FROM sessions"):
            out["sessions"].append(dict(r, profile=profile))
    except sqlite3.Error as exc:
        out["errors"].append("%s: %s" % (path, exc))
board = os.path.join(root, "kanban.db")
if os.path.exists(board):
    try:
        conn = ro(board)
        has_origins = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='kanban_task_origins'").fetchone()
        join = " LEFT JOIN kanban_task_origins o ON o.task_id = t.id" if has_origins else ""
        origin = "o.origin" if has_origins else "NULL"
        for r in conn.execute("SELECT t.id, t.title, t.body, t.session_id, t.idempotency_key, %s AS origin FROM tasks t%s"
                              % (origin, join)):
            out["cards"].append(dict(r))
        for r in conn.execute("SELECT task_id, profile, started_at, metadata FROM task_runs"):
            out["runs"].append(dict(r))
    except sqlite3.Error as exc:
        out["errors"].append("%s: %s" % (board, exc))
print(json.dumps(out))
"""


def capabilities() -> tuple[str, ...]:
    """Every capability row: each job in the two cron rosters by its id, then the fixed rows."""
    jobs = []
    for roster in ROSTERS:
        jobs += [job["id"] for job in json.loads(roster.read_text())["jobs"]]
    return tuple(jobs) + EXTRA_ROWS


def read_stores(context: str, namespace: str) -> dict[str, Any]:
    """The sessions, cards and card runs the gateway pod's stores hold."""
    pod = operations._gateway_pod(context, namespace)
    if not pod:
        raise kube.CommandError("no gateway pod to read the session records in")
    out = kube.kubectl(context, "exec", "-n", namespace, pod, "-c", operations.AGENT_CONTAINER, "--",
                       operations.JOB_PYTHON, "-c", READ_SCRIPT, DATA_ROOT, timeout=READ_TIMEOUT_SECONDS)
    return json.loads(out)


def tokens(session: dict[str, Any]) -> dict[str, int]:
    """A session's tokens; ``input`` excludes the cached input, as Hermes stores it."""
    return {"input": int(session.get("input_tokens") or 0), "cached": int(session.get("cache_read_tokens") or 0),
            "output": int(session.get("output_tokens") or 0),
            "reasoning": int(session.get("reasoning_tokens") or 0)}


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
    result = Join({}, {})

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
                return decide(creator, seen | {sid})[0], "card creator"
            if card.get("origin") in ORIGINS:
                return ORIGINS[card["origin"]], "card origin"
            return UNATTRIBUTED, "card with no creator or origin"
        if EVENT_ID.match(sid):
            return INCIDENT_TRIAGE, "event session"
        parent = session.get("parent_session_id")
        if parent and parent not in seen and parent != sid:
            return decide(parent, seen | {sid})[0], "parent session"
        if session.get("source") in CHAT_SOURCES:
            return CHAT, f"source {session.get('source')}"
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
        row = rows.setdefault(cap, {**dict.fromkeys(TOKEN_FIELDS, 0), "sessions": 0})
        for name, value in tokens(session).items():
            row[name] += value
        row["sessions"] += 1
        if cap == UNATTRIBUTED:
            rule = joined.rules[session["id"]]
            unattributed_rules[rule] = unattributed_rules.get(rule, 0) + 1
    total = sum(r["input"] + r["cached"] + r["output"] for r in rows.values())
    return {"capabilities": rows, "overview": overview(rows, total), "total_tokens": total,
            "unattributed_reasons": unattributed_rules, "checks": checks(rows, total, litellm),
            "errors": list(stores.get("errors", []))}


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


def checks(rows: dict[str, dict[str, int]], total: int, litellm: dict[str, float] | None) -> dict[str, Any]:
    """The design's two checks. The sum is compared input and output apart, cached input counted once."""
    unattributed = rows.get(UNATTRIBUTED, {})
    share = (unattributed.get("input", 0) + unattributed.get("cached", 0) + unattributed.get("output", 0)) / total \
        if total else 0.0
    result: dict[str, Any] = {"unattributed": {"share": round(share, 4), "passed": share < UNATTRIBUTED_LIMIT}}
    if litellm is None:
        result["sum"] = {"passed": False, "reason": "no LiteLLM counters for the window"}
        return result
    ours = {"input": sum(r["input"] + r["cached"] for r in rows.values()),
            "output": sum(r["output"] for r in rows.values())}
    theirs = {"input": float(litellm.get("tokens_input", 0)), "output": float(litellm.get("tokens_output", 0))}
    # No traffic on either side is a sound window; traffic on one side only is not.
    ratios = {k: round(ours[k] / theirs[k], 4) if theirs[k] else (1.0 if not ours[k] else None) for k in ours}
    passed = all(r is not None and abs(r - 1) <= SUM_TOLERANCE for r in ratios.values())
    result["sum"] = {"sessions": ours, "litellm": theirs, "ratio": ratios, "passed": passed}
    return result


def markdown(record: dict[str, dict[str, Any]]) -> str:
    """The token overview and a row for every capability, for each window in ``record``."""
    lines = ["# Model tokens by capability", ""]
    for window, body in record.items():
        sound = all(c.get("passed") for c in body["checks"].values())
        lines += [f"## {window} window ({'sound' if sound else 'unsound'})", "",
                  "| Capability | Tokens | Share |", "| --- | ---: | ---: |"]
        lines += [f"| {r['capability']} | {r['tokens']:,} | {r['share']:.1%} |" for r in body["overview"]]
        lines += ["", "| Capability | Sessions | Input | Cached input | Output |", "| --- | ---: | ---: | ---: | ---: |"]
        for cap, row in sorted(body["capabilities"].items()):
            lines.append(f"| {cap} | {row['sessions']} | {row['input']:,} | {row['cached']:,} | {row['output']:,} |")
        lines += ["", f"Checks: `{json.dumps(body['checks'])}`", ""]
    return "\n".join(lines)
