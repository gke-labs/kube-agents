#!/usr/bin/env python3
"""File a kanban card asking the Platform Agent to run one of its cron jobs.

Why this exists
---------------
Cron ticking is a property of a running *gateway*, and gateways are per
profile. Only the `default` (Chat Agent) profile has one, so every job in
`agents/platform/cron/jobs.json` has sat in its roster without ever firing.

Moving those jobs into this profile's roster verbatim would not fix it. The
Chat Agent's toolsets are deliberately stripped to `mcp-router`, `kanban` and
`memory` (`agents/chat/config.yaml`): no `terminal`, so no kubectl or gcloud;
no `skills`, so `"skills": ["fleet-audit"]` could not even resolve. It cannot
do the work. What moves here is the **trigger**, not the work.

Each of those jobs therefore gets a `no_agent` entry in this profile's roster —
a plain subprocess, run outside the toolset denylist entirely — pointing at a
thin wrapper that calls `main()` below with one job id. There is a wrapper per
job rather than one shared script because the scheduler invokes a script as
`[interpreter, path]` — no arguments, and nothing in the environment naming the
job — so the file itself is the only place the id can live. The tick files a
single
kanban card assigned to `platform`; the gateway dispatcher spawns a Platform
Agent worker on it with the full platform toolset, and the card asks for
exactly one thing: `cronjob(action='run', job_id=...)`.

That indirection is the point. The card names the job instead of carrying a
copy of its prompt, so `agents/platform/cron/jobs.json` stays the single
definition of what each audit does, and the dispatched run gets that job's own
prompt, skills, model and turn budget. It is the same reasoning
`agents/platform/AGENTS.md` gives for never re-enacting a scheduled job's work
by hand: an improvised re-enactment gets none of them.

Delivery
--------
These cards complete silently, by construction. The gateway notifier iterates
rows in `kanban_notify_subs`, which are written at `kanban_create` time from
the originating chat session's identity — and a cron script has no session, so
no row is written and nothing posts to chat when the card finishes. That suits
the five audits, whose deliverable is the `fleet-audit` ledger issue rather
than a chat message, and `github-issue-resolver`, which answers on the issues
it resolves. If chat delivery is ever wanted here the missing piece is a
subscription row, not a change to this script — see
`agents/platform/scripts/kanban_notify_propagate.py`, which does exactly that
copy for a worker's child cards.

Stdout is the wire to chat
--------------------------
For a `no_agent` job the scheduler delivers the script's stdout verbatim as the
run's message, and treats empty stdout as a silent run. Every message here goes
to stderr and a successful tick prints nothing at all. The one exception is
deliberate: a non-zero exit is delivered as a watchdog alert, which is what
should happen when the thing that starts the audits stops working.
"""

import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

# Where the Platform Agent's cron roster can be found from inside this pod. The
# scaffolded profile copy is preferred because it is what the dispatched run
# will actually execute; the baked template is the fallback for a pod whose
# profile has not been scaffolded yet. Reading the roster rather than restating
# it here means a job renamed in one place cannot go stale in the other.
ROSTER_PATHS = (
    Path("/opt/data/profiles/platform/cron/jobs.json"),
    Path("/opt/platform-template/cron/jobs.json"),
)

ASSIGNEE = "platform"

# Statuses that mean an earlier card for this job is still in flight. Filing a
# second one would run the same audit concurrently with itself — two runs
# writing the same ledger issue, or two `github-issue-resolver` passes racing
# for the same issue.
#
# `blocked` is deliberately absent. A card blocks when its worker tripped the
# failure breaker or escalated for input, and it then stays blocked until a
# human clears it. Counting that as in-flight would let one bad run switch the
# audit off indefinitely, silently, which is the failure mode this whole change
# exists to end. A blocked card is a thing to go and look at, not a lock.
IN_FLIGHT = frozenset({"triage", "todo", "ready", "scheduled", "running", "review"})

# Per-task runtime cap handed to the dispatcher, which SIGTERMs the worker when
# it is exceeded. The audits are genuinely long — the platform profile raises
# `agent.max_turns` to 250 for them — so they get hours. `github-issue-resolver`
# is capped under its own 30-minute period so a wedged run cannot sit on the
# board across two ticks.
MAX_RUNTIME = {"github-issue-resolver": "25m"}
DEFAULT_MAX_RUNTIME = "2h"


def log(msg: str) -> None:
    """Write to stderr — stdout is the delivery channel and must stay empty."""
    sys.stderr.write(f"platform_cron_dispatch: {msg}\n")


def load_roster(paths=ROSTER_PATHS) -> dict:
    """Return {job_id: job} from the first readable Platform Agent roster."""
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - try the next path, report at the end
            continue
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        return {str(j.get("id")): j for j in jobs if j.get("id")}
    return {}


def card_title(job_name: str) -> str:
    """The card's title, which is also its dedup handle.

    Deterministic per job because `has_open_card` matches on it: the board's
    JSON listing exposes the title but not the idempotency key, so the title is
    the only stable thing to recognise an earlier card by.
    """
    return f"Run the {job_name} cron job"


def idempotency_key(job_id: str, now: datetime) -> str:
    """Dedup key for one tick, bucketed to the minute.

    Guards the narrow case the board can guard: two dispatches of the same tick
    (a scheduler retry, or a second gateway process) landing within the same
    minute. Anything wider would be wrong — a fixed per-job key would match the
    first card forever and the job would run exactly once, ever, since the key
    dedups against every non-archived task.
    """
    return f"{job_id}-{now.strftime('%Y%m%dT%H%M')}"


def _run_slash(cmd: str) -> str:
    from hermes_cli.kanban import run_slash  # type: ignore import-not-found

    return str(run_slash(cmd)).strip()


def has_open_card(title: str) -> bool:
    """True when a card with this title is still working.

    Fails **open**: if the board cannot be read we file anyway. A duplicate
    audit run is a bad afternoon; a discovery that the audits stopped months
    ago because a listing error was treated as "already running" is worse.
    """
    try:
        raw = _run_slash(f"list --json --assignee {shlex.quote(ASSIGNEE)}")
        tasks = json.loads(raw) if raw else []
    except Exception as e:  # noqa: BLE001
        log(f"could not read the board ({e}) — filing anyway")
        return False
    for task in tasks:
        if task.get("title") == title and str(task.get("status")) in IN_FLIGHT:
            log(f"{task.get('id')} is still {task.get('status')} — skipping this tick")
            return True
    return False


def card_body(job_id: str) -> str:
    return f"""Dispatch this one scheduled job and report what it produced:

    cronjob(action='run', job_id='{job_id}')

That job and no other. The call is synchronous: it returns when the run
finishes, carrying the run's own closing report in `response` and the path of
its saved output in `output_file`.

**Do not re-enact the job's work in this session.** A dispatched run gets that
job's prompt, skills, model and turn budget from the Platform Agent's cron
roster; anything improvised here gets none of them.

Then `kanban_complete` with a summary of what the run produced, spelling out
every URL in full. A run that answers `[SILENT]` has suppressed its own
delivery on the assumption nobody was watching — read the `output_file` it
names and summarise that instead of reporting silence.

Nobody is watching this card in chat. It was filed by a cron script, which has
no chat session and so no notification subscription; your completion posts
nowhere. The run's real deliverable is its ledger issue, or the issues it
resolves. Your summary is for the board's record and for whoever comes looking
when a schedule appears to have stopped.
"""


def file_card(job_id: str, job_name: str, now: datetime) -> str | None:
    """File the dispatch card. Returns its id, or None if nothing was filed."""
    title = card_title(job_name)
    if has_open_card(title):
        return None

    cmd = (
        f"create --json --assignee {shlex.quote(ASSIGNEE)} "
        f"--created-by {shlex.quote('cron')} "
        f"--max-runtime {shlex.quote(MAX_RUNTIME.get(job_id, DEFAULT_MAX_RUNTIME))} "
        f"--idempotency-key {shlex.quote(idempotency_key(job_id, now))} "
        f"--body {shlex.quote(card_body(job_id))} "
        f"{shlex.quote(title)}"
    )
    try:
        out = _run_slash(cmd)
    except Exception as e:  # noqa: BLE001 - never fail the cron run on board trouble
        log(f"could not file the card for {job_id}: {e}")
        return None

    task_id = _parse_task_id(out)
    if not task_id:
        log(f"filed {job_id} but could not read a task id from: {out}")
        return None
    log(f"filed {task_id} to run {job_id}")
    return task_id


def _parse_task_id(out: str) -> str | None:
    """Pull the task id out of `kanban create --json` output.

    Mirrors bootstrap_scan_gate's parser: the JSON object is located rather
    than assumed to be the whole of stdout, because the CLI prints tips
    alongside it.
    """
    start = out.find("{")
    end = out.rfind("}")
    if start != -1 and end > start:
        try:
            return str(json.loads(out[start : end + 1]).get("id") or "") or None
        except Exception:  # noqa: BLE001
            pass
    import re

    m = re.search(r"\b(t_[0-9a-f]+)\b", out)
    return m.group(1) if m else None


def main(job_id: str, roster_paths=ROSTER_PATHS) -> int:
    roster = load_roster(roster_paths)
    if not roster:
        # No roster means no job definitions to dispatch against, and a card
        # naming an unknown id would burn a worker spawn to be told so. Exit
        # non-zero: this is the watchdog itself being broken, and the scheduler
        # turns a non-zero exit into an alert.
        log(f"no Platform Agent cron roster found at any of {list(ROSTER_PATHS)}")
        return 1
    job = roster.get(job_id)
    if job is None:
        log(f"{job_id!r} is not in the Platform Agent cron roster — nothing to run")
        return 1
    if not job.get("enabled", True):
        log(f"{job_id} is disabled in the Platform Agent roster — skipping")
        return 0  # silent: disabling the job there should disable it here too

    file_card(job_id, str(job.get("name") or job_id), datetime.now(timezone.utc))
    # Stdout stays empty on purpose — the card is the output, not a chat message.
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the wrapper scripts
    if len(sys.argv) != 2:
        sys.stderr.write(f"usage: {os.path.basename(sys.argv[0])} <job-id>\n")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
