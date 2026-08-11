#!/usr/bin/env python3
"""File a kanban card that asks the Platform Agent to do one scheduled job.

Why this exists
---------------
Cron ticking is a property of a running *gateway*, and gateways are per
profile. Only the `default` (Chat Agent) profile has one, so every job in
`agents/platform/cron/jobs.json` sat in its roster without ever firing. That
roster is now empty: all six governance jobs live in this profile's
`cron/jobs.json`, which is the roster the one running ticker reads.

Moving them cannot mean running them here. The Chat Agent's toolsets are
deliberately stripped to `mcp-router`, `kanban` and `memory`
(`agents/chat/config.yaml`): no `terminal`, so no kubectl or gcloud; no
`skills`, so `fleet-audit` could not even resolve. So each job is a `no_agent`
entry — a plain subprocess, run outside the toolset denylist entirely, which
prompts no model. Its whole job is to file one kanban card assigned to
`platform`. The gateway dispatcher spawns a Platform Agent worker on that card,
with the full platform toolset, and the worker does the audit.

The job's `prompt` is what the card carries. That is the one copy of it: the
schedule, the prompt, the skills and the `enabled` flag are all in the roster
entry, and this script reads them back out at tick time rather than restating
any of them. Nothing about a job is written twice, so nothing about a job can
go stale against itself.

There is a wrapper script per job rather than one shared script because the
scheduler invokes a script as `[interpreter, path]` — no arguments, and nothing
in the environment naming the job (`cron/scheduler.py:_run_job_script`). The
wrapper's five lines are the only place the id can live, and an id is all they
carry.

What the worker does and does not inherit
-----------------------------------------
A dispatched card is a kanban worker, not a cron run, so the scheduler's
per-job knobs do not reach it. In practice that costs nothing here: none of the
six pins a `model`, and `agent.max_turns: 250` is set on the platform profile
itself (`agents/platform/config.yaml`).

`skills` is the exception, and it is carried across rather than lost. The
scheduler force-loaded a job's skills by resolving them through `skill_view`
and prepending their content to the prompt
(`cron/scheduler.py:_build_job_prompt`). The card reaches the same place by a
different road: `kanban create --skill <name>` is persisted on the task, the
dispatcher spawns the worker with `--skills <name>`, and
`build_preloaded_skills_prompt` puts the skill's text in the session before its
first turn. Leaving it to the card body to *mention* the skill would have made
loading it the worker's decision, and a run that decides not to is the one that
hand-writes findings it never gathered — 2026-08-03, five audits in one turn,
a fleet-wide all-clear nobody had looked for.

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
should happen when the thing that starts the audits stops working. That is also
why these entries ship `deliver: "all"` and not `deliver: "local"` — `local`
resolves to no delivery target at all (`scheduler.py:_deliver_result`), so the
alert would be built and then dropped, which is indistinguishable from the
audits quietly never running again. `main` alerts when the board answered a
listing and then refused the create — the board is demonstrably up, so that is
a defect in the request, not weather.

Getting the jobs onto an existing cluster
-----------------------------------------
`docker-entrypoint.sh` syncs `/opt/defaults` onto the PVC with `cp -ru` — which
copies only when the *source* is newer. The scheduler writes `last_run_at` back
into the volume's copy on every tick, so the destination is permanently newer
and a new entry would never be copied; the force-sync beside it covers
`config.yaml SOUL.md AGENTS.md CAPABILITIES.md` and not `cron/`. What carries a
change to these entries onto a volume that already exists is step 2c of the
entrypoint, which runs `cron_jobs_sync.py` to merge this file into the volume's
copy by job id. Edit the entry here and it travels on the next roll.

The one thing that does not travel is a deletion. Neither reconcile prunes: this
one never removes an id the image stops declaring, on the grounds that it may
have been added by hand, and `profile_scaffold.merge_cron_store` adds and
overwrites but never prunes either. So retire an entry by shipping
`enabled: false` and leaving it in place. The six platform-side entries this
script replaced are the worked example — a cluster provisioned before they moved
still has them under `profiles/platform/cron/jobs.json`, and they come off that
file by hand or not at all.
"""

import json
import os
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

# Where this profile's own cron roster can be found from inside this pod. The
# live copy is preferred because it is the file the scheduler is ticking from,
# so a job disabled at runtime stops dispatching too; the baked defaults are
# the fallback for a pod whose volume has not been seeded yet. Reading the
# roster rather than restating it here is what keeps the prompt single-homed.
ROSTER_PATHS = (
    Path(os.environ.get("HERMES_HOME", "/opt/data")) / "cron/jobs.json",
    Path("/opt/defaults/cron/jobs.json"),
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

# ...but "not a lock" needs a floor, or it is a leak. Blocked cards are also
# never archived (see KEEP_FINISHED), so a job that blocks every run has nothing
# stopping it: `github-issue-resolver` would file 48 cards a day, spawn a worker
# for each with a fresh `consecutive_failures` counter, block, and repeat,
# indefinitely and without ever exiting non-zero.
#
# Past this many, stop filing and alert. That keeps the property — one blocked
# card, or two, does not stop the schedule — while turning a job that only ever
# blocks into the page it should have been, since by then nobody is being
# deprived of a run that would have worked. Deliberately larger than the two or
# three a genuinely stuck job accumulates before someone looks.
MAX_BLOCKED = 5

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


def load_roster(paths=ROSTER_PATHS, job_id: str | None = None) -> dict:
    """Return {job_id: job} from the first copy of this cron roster that has it.

    The paths are ordered live-volume first, image-defaults second, and the
    fallback only earns its place if "readable" is not the test. The volume's
    copy is written by the scheduler on every tick and is never refreshed from
    the image afterwards — the entrypoint's `cp -ru` skips a destination that is
    permanently newer — so on an upgraded cluster it exists, parses, and is
    missing exactly the entries this release added. Taking the first readable
    path would hand back that stale roster and the image's copy, which does
    carry the entry, would never be opened.

    Which is the one case the fallback exists for: `platform_cron_dispatch.py
    <job-id>` is the on-demand route `agents/platform/AGENTS.md` and
    `fleet-audit/SKILL.md` send the agent down, and it is reached for precisely
    when the schedule is missing.

    Without `job_id` there is nothing to search for, so the first readable copy
    is the answer — which is also what the caller wants for its "not in the
    roster" message: the roster the operator's cluster is actually running.
    """
    first: dict | None = None
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - try the next path, report at the end
            continue
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        roster = {str(j.get("id")): j for j in jobs if j.get("id")}
        if job_id is None or job_id in roster:
            return roster
        if first is None:
            first = roster
    return first if first is not None else {}


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
    return f"{job_name} [{job_id}]"


def _is_this_jobs_card(task: dict, job_id: str, job_name: str) -> bool:
    """Recognise a card this job filed, under the current or an older title.

    `CREATED_BY` is checked first and is what makes the title match safe: the
    bare-name form is a title a person can cause the Platform Agent to file,
    the bracketed one is not, but neither is worth trusting on its own.

    The bracketed id is the real handle, and it covers both the current title
    and the `Run the <name> cron job [<id>]` form used while the card was an
    indirection to a platform-side cron entry rather than the work itself. The
    bare-name form is accepted too so that the cards filed before the id was
    added stay sweepable — without it, adding the id would strand exactly the
    backlog it exists to prevent. Every such card carries this creator, because
    `--created-by` was there from the first version of this script, so the two
    guards agree on the whole backlog.
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


def _find_json(out: str, opener: str, kind: type):
    """The first JSON value of `kind` in `out`, or None if there is none.

    `--json` is asked for, but it does not buy a clean payload: `run_slash`
    redirects `sys.stderr` into the buffer it hands back for the duration of
    the call, so whatever the board wrote alongside the answer — a
    `DeprecationWarning`, a sqlite adapter notice, a `kanban:` diagnostic — is
    glued to it. Parsing the whole string turns any of those into a failure,
    and since the noise is a property of the deployment rather than of the
    tick, it is the same failure every tick from then on.

    Scanned with `raw_decode` instead of sliced between the first opener and
    the last closer, because the noise brings brackets of its own: a
    `[ROUTER-MCP] cannot list profiles …` line in front of the payload defeats
    the slice and not this. Candidates that do not parse, or that parse as the
    wrong shape, are stepped over rather than accepted.
    """
    decoder = json.JSONDecoder()
    idx = out.find(opener)
    while idx != -1:
        try:
            value = decoder.raw_decode(out, idx)[0]
        except ValueError:
            value = None
        if isinstance(value, kind):
            return value
        idx = out.find(opener, idx + 1)
    return None


def survey_board(job_id: str, job_name: str) -> tuple[bool, list[str], bool, int]:
    """One listing, four answers about this job's earlier cards.

    Returns `(in_flight, stale, reachable, blocked)` — whether a card of this
    job's is still working, the ids of its finished cards beyond
    `KEEP_FINISHED` oldest first, whether the board answered at all, and how
    many of its cards are sitting blocked.

    Fails **open** on the first two: an unreadable board reports nothing in
    flight and nothing to archive, so the tick files its card. A duplicate
    audit run is a bad afternoon; discovering months later that the audits
    stopped because a listing error read as "already running" is worse. It
    reports no blocked cards for the same reason — a count nobody could read is
    not grounds for holding the schedule.

    `reachable` is what stops that leniency swallowing a real defect. It lets
    the caller tell "the board is down" — transient, retry next tick, stay
    quiet — from "the board is up and rejected our create", which is a bad
    request that will fail identically forever.
    """
    try:
        raw = _run_slash(f"list --json --assignee {shlex.quote(ASSIGNEE)}")
    except ImportError:
        # Not weather, and the one exception `_run_slash` can actually raise:
        # it imports `hermes_cli.kanban` on every call, so this is the CLI
        # being absent, renamed, or invisible to the interpreter the scheduler
        # chose. It will fail identically on every future tick. Failing open
        # here would retire all six audits behind one stderr line.
        raise
    except Exception as e:  # noqa: BLE001
        log(f"could not read the board ({e}) — filing anyway")
        return False, [], False, 0

    tasks = [] if not raw else _find_json(raw, "[", list)
    if tasks is None:
        # Both dedup layers are now blind, and they stay blind: see `_find_json`
        # for why an unparseable listing is a standing condition rather than a
        # blip. Say so with the payload attached — this is the difference
        # between a one-line fix and months of concurrent audits and an
        # unbounded board.
        log(f"could not parse the board listing — filing anyway: {raw[:300]}")
        return False, [], False, 0

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
            return True, [], True, 0

    finished = sorted(
        (t for t in mine if str(t.get("status")) in FINISHED),
        key=_created_at,
    )
    surplus = finished[:-KEEP_FINISHED] if KEEP_FINISHED > 0 else finished
    blocked = sum(1 for t in mine if str(t.get("status")) == "blocked")
    return False, [str(t["id"]) for t in surplus if t.get("id")], True, blocked


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


def job_skills(job: dict) -> list[str]:
    """The entry's skill names, cleaned. One reading for the flags and the body.

    Both callers have to agree: a name force-loaded onto the worker that the
    card body does not mention, or the reverse, is the kind of drift the single
    roster entry exists to rule out.
    """
    return [s for s in (str(x).strip() for x in (job.get("skills") or [])) if s]


def card_body(job: dict) -> str:
    """The card's instructions: the job's own prompt, plus how to close out.

    The prompt is copied off the roster entry rather than restated here, so
    editing `cron/jobs.json` is the whole of editing what a watchdog does.

    `skills` is named below even though `file_card` force-loads them onto the
    worker and every shipped prompt already names its skill in prose. The card
    is the durable record of what the job expected: a person reading the board
    sees it, and so does a run whose preload resolved nothing, which
    `build_preloaded_skills_prompt` reports as missing rather than fatal.
    """
    parts = [str(job.get("prompt") or "").strip()]
    skills = job_skills(job)
    if skills:
        parts.append(
            "Skills for this job: " + ", ".join(f"`{s}`" for s in skills) + "."
        )
    parts.append(
        "Then `kanban_complete` with a summary of what you produced, spelling out "
        "every URL in full."
    )
    parts.append(
        "Nobody is watching this card in chat. It was filed by a cron script, which "
        "has no chat session and so no notification subscription; your completion "
        "posts nowhere. This job's real deliverable is its ledger issue, or the "
        "issues it resolves. Your summary is for the board's record and for whoever "
        "comes looking when a schedule appears to have stopped."
    )
    return "\n\n".join(parts) + "\n"


def file_card(job_id: str, job: dict, now: datetime) -> bool:
    """File the card for one job. Returns False only if the tick should alert.

    Filing nothing is not by itself a failure — an in-flight card is the guard
    working, and a board that would not answer a listing is weather. The one
    case worth waking someone for is a board that answered the listing and then
    refused the create: it is up, it is talking, and it said no. That will say
    no identically on every future tick, so a quiet exit would retire the audit
    permanently and leave one stderr line as the only trace.

    The blocked-card cap alerts for the mirror-image reason: a job that blocks
    every run files forever and never fails, so the one signal it produces is a
    board filling up. See `MAX_BLOCKED`.

    Refusal is read off the *return value*, not off an exception, because
    `hermes_cli.kanban.run_slash` does not raise: it wraps the subcommand in
    `except SystemExit: pass` / `except Exception`, discards the return code,
    and hands back whatever the two buffers captured. Every way the board can
    say no — an argparse rejection, `_cmd_create`'s `kanban: …` guards, a
    locked database — therefore arrives here as an ordinary string. Waiting for
    a raise means waiting for something that cannot happen, which is exactly
    how a refused create used to exit 0 while logging that the card was filed.
    """
    job_name = str(job.get("name") or job_id)
    title = card_title(job_id, job_name)
    in_flight, stale, reachable, blocked = survey_board(job_id, job_name)
    if in_flight:
        return True
    if blocked >= MAX_BLOCKED:
        # The other kind of permanent failure: a job whose every run blocks
        # would otherwise file forever and alert never. Stop, and say why —
        # the cards are already on the board for whoever answers.
        log(
            f"{blocked} blocked card(s) for {job_id} (cap {MAX_BLOCKED}) — not "
            f"filing another; clear or archive them to resume the schedule"
        )
        archive_cards(stale)
        return False

    # Before `--body`, because the title is positional and has to stay last.
    skill_flags = "".join(
        f"--skill {shlex.quote(s)} " for s in job_skills(job)
    )
    cmd = (
        f"create --json --assignee {shlex.quote(ASSIGNEE)} "
        f"--created-by {shlex.quote(CREATED_BY)} "
        f"--max-runtime {shlex.quote(MAX_RUNTIME.get(job_id, DEFAULT_MAX_RUNTIME))} "
        f"--idempotency-key {shlex.quote(idempotency_key(job_id, now))} "
        f"{skill_flags}"
        f"--body {shlex.quote(card_body(job))} "
        f"{shlex.quote(title)}"
    )
    try:
        out = _run_slash(cmd)
    except ImportError:
        raise  # See survey_board: the CLI being gone is a defect, not weather.
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
        # No task object came back, so no card was created — see
        # `_parse_task_id` for why that reading is safe under `--json`. Same
        # verdict as a create that failed loudly: alert if the listing had just
        # succeeded, stay quiet if the whole board was already unreachable.
        log(f"the board refused the create for {job_id}: {out.strip()[:300]}")
        return not reachable
    log(f"filed {task_id} to run {job_id}")
    return True


def _parse_task_id(out: str) -> str | None:
    """Pull the task id out of `kanban create --json` output, or None.

    The object is located rather than assumed to be the whole of stdout,
    because the CLI prints tips alongside it and `run_slash` folds stderr into
    the same buffer — see `_find_json`.

    None means the create did not happen. That is a stronger claim than "the
    id was unreadable", and it holds only because this script always passes
    `--json`: on that path `_cmd_create` prints one `json.dumps` of the task
    and nothing else, while every refusal prints `kanban: …` to stderr and
    returns 2 without touching stdout. So no JSON object means no card.

    Which is why there is no `t_[0-9a-f]+` fallback, tempting as it looks next
    to the `Created <id>` form: that form is only printed in the *non*-json
    branch, so here the pattern could only ever match an id quoted back inside
    an error — "duplicate of t_abc12345" — and reading that as success would
    reinstate the silent failure this parser exists to detect.
    """
    task = _find_json(out, "{", dict)
    if task is None:
        return None
    return str(task.get("id") or "") or None


def main(job_id: str, roster_paths=ROSTER_PATHS) -> int:
    roster = load_roster(roster_paths, job_id)
    if not roster:
        # No roster means no prompt to put on a card, and a card with an empty
        # body would burn a worker spawn to say so. Exit non-zero: this is the
        # watchdog itself being broken, and the scheduler turns a non-zero exit
        # into an alert.
        log(f"no cron roster found at any of {list(roster_paths)}")
        return 1
    job = roster.get(job_id)
    if job is None:
        log(f"{job_id!r} is not in the cron roster — nothing to dispatch")
        return 1
    if not job.get("enabled", True):
        # Not reachable on a normal tick — the scheduler does not run a
        # disabled job — so this only answers a hand-run of the wrapper.
        log(f"{job_id} is disabled — skipping")
        return 0
    if not str(job.get("prompt") or "").strip():
        # The prompt IS the work now that nothing else holds a copy of it. An
        # entry without one would file a card that says nothing, and the worker
        # would either improvise the audit or give up; both are worse than an
        # alert naming the job whose definition lost its body.
        log(f"{job_id} has no prompt — nothing to put on the card")
        return 1

    try:
        ok = file_card(job_id, job, datetime.now(timezone.utc))
    except ImportError as e:
        # The kanban CLI is what this script is made of; there is no degraded
        # mode to retry into. A Hermes upgrade that moves `hermes_cli.kanban`
        # would otherwise stop every audit and say so only on a stderr stream
        # nobody reads.
        log(f"the kanban CLI is not importable ({e}) — no card can be filed")
        return 1
    # Stdout stays empty on purpose — the card is the output, not a chat message.
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - exercised via the wrapper scripts
    if len(sys.argv) != 2:
        sys.stderr.write(f"usage: {os.path.basename(sys.argv[0])} <job-id>\n")
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
