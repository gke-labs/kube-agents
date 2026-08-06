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
should happen when the thing that starts the audits stops working. `main`
alerts when the board answered a listing and then refused the create — the
board is demonstrably up, so that is a defect in the request, not weather.

Getting the triggers onto an existing cluster
---------------------------------------------
The `dispatch-*` entries this script backs live in the image's
`cron/jobs.json`, and `docker-entrypoint.sh` syncs `/opt/defaults` onto the PVC
with `cp -ru` — which copies only when the *source* is newer. The scheduler
writes `last_run_at` back into the volume's copy on every tick, so the
destination is permanently newer and the new entries are never copied; the
force-sync beside it covers `config.yaml SOUL.md AGENTS.md CAPABILITIES.md`
and not `cron/`. On today's `main` these triggers therefore reach a fresh
volume only. gke-labs/kube-agents#528 adds the per-id merge that fixes it for
existing ones; until it lands, an upgraded cluster needs the entries put on the
volume by hand.
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

# Stamped on every card this script files (`--created-by`), and required of
# every card it claims back. Titles alone are not enough to tell them apart:
# `agents/platform/AGENTS.md` tells the Platform Agent to file "Run the <name>
# cron job" when a person asks for a job by hand, which is the bare-name form
# below exactly. Without this, one "run all the fleet audits" request would
# hand the sweep a human's cards to archive. `_profile_author()` stamps those
# with the profile name, so the two never collide.
CREATED_BY = "cron"

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

# Statuses that mean the card is over and its record is all that is left.
# `completed` is here because it is real, not as a defensive guess: live boards
# carry it, and it is absent from `kanban_db.VALID_STATUSES`.
FINISHED = frozenset({"done", "completed"})

# Everything above, plus the two statuses deliberately handled by doing nothing:
# a `blocked` card waits on a person, an `archived` one is already swept.
#
# A status outside this set is logged rather than passed over, because the
# board's vocabulary is not schema-constrained — `VALID_STATUSES` gates a query
# *filter*, not a write, and `completed` is the proof that writes escape it. An
# unrecognised status falls through both guards above: if it meant "running"
# the audit would run concurrently with itself, and if it meant "finished" the
# card would never be archived.
#
# Logging, and not a stricter default, is the fix. Treating the unknown as
# in-flight would let one unrecognised card switch an audit off permanently,
# which is worse than either failure and is the exact outcome this bridge
# exists to end. So the behaviour stays lenient and stops being invisible.
KNOWN_STATUSES = IN_FLIGHT | FINISHED | frozenset({"blocked", "archived"})

# How many finished cards per job to leave on the board. Every tick files one,
# and nothing else ever clears them: `github-issue-resolver` alone lands 48 a
# day, so a board left to itself buries the two cards a week that carry a
# weekly audit's result. Three is enough to answer "did the last few runs go
# through?" at a glance and to diff a bad run against its predecessor. The rest
# are archived rather than purged — `kanban list --archived` still has them,
# and `kanban gc` is what actually reclaims the space.
#
# Blocked cards are never archived here. One is the only durable sign that a
# job needs a human, and this bridge deliberately keeps scheduling past it.
KEEP_FINISHED = 3

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


def card_title(job_id: str, job_name: str) -> str:
    """The card's title, which is also its dedup handle.

    Deterministic per job because `survey_board` matches on it: the board's
    JSON listing exposes the title but not the idempotency key, so the title is
    the only stable thing to recognise an earlier card by.

    The id is in it because the *name* is not stable and the reference docs say
    so outright — `obtainability-audit` is now the Workload Reliability Audit.
    Keying on the name alone would mean that the tick after a rename cannot see
    the run currently in flight (so it files a concurrent duplicate), and that
    every card filed under the old name is unsweepable forever, because no
    future tick will ever match its title again.
    """
    return f"Run the {job_name} cron job [{job_id}]"


def _is_this_jobs_card(task: dict, job_id: str, job_name: str) -> bool:
    """Recognise a card this job filed, under the current or the old title.

    `CREATED_BY` is checked first and is what makes the title match safe: the
    bare-name form is a title a person can cause the Platform Agent to file,
    the bracketed one is not, but neither is worth trusting on its own.

    The bracketed id is the real handle. The bare-name form is accepted too so
    that the cards filed before the id was added stay sweepable — without it,
    adding the id would strand exactly the backlog it exists to prevent. Every
    such card carries this creator, because `--created-by` was there from the
    first version of this script, so the two guards agree on the whole backlog.
    """
    if str(task.get("created_by") or "") != CREATED_BY:
        return False
    title = str(task.get("title") or "")
    return title.endswith(f"[{job_id}]") or title == f"Run the {job_name} cron job"


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


def survey_board(job_id: str, job_name: str) -> tuple[bool, list[str], bool]:
    """One listing, three answers about this job's earlier cards.

    Returns `(in_flight, stale, reachable)` — whether a card of this job's is
    still working, the ids of its finished cards beyond `KEEP_FINISHED` oldest
    first, and whether the board answered at all.

    Fails **open** on the first two: an unreadable board reports nothing in
    flight and nothing to archive, so the tick files its card. A duplicate
    audit run is a bad afternoon; discovering months later that the audits
    stopped because a listing error read as "already running" is worse.

    `reachable` is what stops that leniency swallowing a real defect. It lets
    the caller tell "the board is down" — transient, retry next tick, stay
    quiet — from "the board is up and rejected our create", which is a bad
    request that will fail identically forever.
    """
    try:
        raw = _run_slash(f"list --json --assignee {shlex.quote(ASSIGNEE)}")
        tasks = json.loads(raw) if raw else []
    except Exception as e:  # noqa: BLE001
        log(f"could not read the board ({e}) — filing anyway")
        return False, [], False

    mine = [
        t
        for t in tasks
        if isinstance(t, dict) and _is_this_jobs_card(t, job_id, job_name)
    ]
    unknown = sorted(
        {
            f"{t.get('id')}={t.get('status')}"
            for t in mine
            if str(t.get("status")) not in KNOWN_STATUSES
        }
    )
    if unknown:
        log(
            f"board reported {len(unknown)} card(s) in a status this script does not "
            f"know ({', '.join(unknown)}) — not counted as in-flight and never "
            f"archived; see KNOWN_STATUSES"
        )

    for task in mine:
        if str(task.get("status")) in IN_FLIGHT:
            log(f"{task.get('id')} is still {task.get('status')} — skipping this tick")
            return True, [], True

    finished = sorted(
        (t for t in mine if str(t.get("status")) in FINISHED),
        key=_created_at,
    )
    surplus = finished[:-KEEP_FINISHED] if KEEP_FINISHED > 0 else finished
    return False, [str(t["id"]) for t in surplus if t.get("id")], True


def _created_at(task: dict) -> float:
    """Sort key over a board row's epoch `created_at`, tolerating junk.

    Coerced rather than trusted because the alternative to a wrong sort order
    is a `TypeError` out of `sorted` on a mixed column, and this runs on the
    path that files the card.
    """
    try:
        return float(task.get("created_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def archive_cards(task_ids: list[str]) -> None:
    """Archive finished cards. Best-effort: a full board is not worth a failed tick.

    The response is counted rather than assumed. `kanban archive` reports one
    `Archived <id>` line per card and writes its refusals to stderr, which
    shares this buffer — so a call that archived nothing still returns
    normally, and logging success on "did not raise" would have the log assert
    the board was being swept while it grew. This log line is the only signal
    anyone gets, so it has to mean what it says.
    """
    if not task_ids:
        return
    try:
        out = _run_slash("archive " + " ".join(shlex.quote(t) for t in task_ids))
    except Exception as e:  # noqa: BLE001
        log(f"could not archive {len(task_ids)} finished card(s): {e}")
        return
    confirmed = out.count("Archived ")
    if confirmed < len(task_ids):
        log(
            f"archive confirmed {confirmed} of {len(task_ids)} card(s) "
            f"({', '.join(task_ids)}); board said: {out.strip()[:300]}"
        )
    else:
        log(f"archived {len(task_ids)} finished card(s): {', '.join(task_ids)}")


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


def file_card(job_id: str, job_name: str, now: datetime) -> bool:
    """File the dispatch card. Returns False only if the tick should alert.

    Filing nothing is not by itself a failure — an in-flight card is the guard
    working, and a board that would not answer a listing is weather. The one
    case worth waking someone for is a board that answered the listing and then
    refused the create: it is up, it is talking, and it said no. That will say
    no identically on every future tick, so a quiet exit would retire the audit
    permanently and leave one stderr line as the only trace.
    """
    title = card_title(job_id, job_name)
    in_flight, stale, reachable = survey_board(job_id, job_name)
    if in_flight:
        return True

    cmd = (
        f"create --json --assignee {shlex.quote(ASSIGNEE)} "
        f"--created-by {shlex.quote(CREATED_BY)} "
        f"--max-runtime {shlex.quote(MAX_RUNTIME.get(job_id, DEFAULT_MAX_RUNTIME))} "
        f"--idempotency-key {shlex.quote(idempotency_key(job_id, now))} "
        f"--body {shlex.quote(card_body(job_id))} "
        f"{shlex.quote(title)}"
    )
    try:
        out = _run_slash(cmd)
    except Exception as e:  # noqa: BLE001 - the board decides whether this alerts
        log(f"could not file the card for {job_id}: {e}")
        # Reachable means the listing just succeeded, so this is the request
        # being wrong, not the board being away. Alert. Otherwise stay quiet
        # and let the next tick retry.
        return not reachable
    finally:
        # Prune whether or not the new card landed. The backlog is this job's
        # own litter and clearing it does not depend on today's tick working.
        archive_cards(stale)

    task_id = _parse_task_id(out)
    if not task_id:
        # The card itself is very likely on the board; only its id is
        # unreadable, and nothing here uses the id afterwards. Not an alert.
        log(f"filed {job_id} but could not read a task id from: {out}")
        return True
    log(f"filed {task_id} to run {job_id}")
    return True


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
        log(f"no Platform Agent cron roster found at any of {list(roster_paths)}")
        return 1
    job = roster.get(job_id)
    if job is None:
        log(f"{job_id!r} is not in the Platform Agent cron roster — nothing to run")
        return 1
    if not job.get("enabled", True):
        log(f"{job_id} is disabled in the Platform Agent roster — skipping")
        return 0  # silent: disabling the job there should disable it here too

    ok = file_card(job_id, str(job.get("name") or job_id), datetime.now(timezone.utc))
    # Stdout stays empty on purpose — the card is the output, not a chat message.
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised via the wrapper scripts
    if len(sys.argv) != 2:
        sys.stderr.write(f"usage: {os.path.basename(sys.argv[0])} <job-id>\n")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
