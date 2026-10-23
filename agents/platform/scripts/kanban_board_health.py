#!/usr/bin/env python3
"""Standing board-health check for the kanban board.

Nothing in this deployment periodically asks whether the board is wedged. Most
of the detection already exists — ``hermes_cli/kanban_diagnostics.py`` ships an
eight-rule engine behind ``hermes kanban diagnostics`` and the dashboard plugin
— but nothing schedules it and nothing pushes its output, so a human notices a
stuck board by accident. This script is the half that asks.

It reports two kinds of thing, and deliberately nothing else.

1. **Invariant violations**, queried here directly. Every non-``running``
   transition in ``hermes_cli/kanban_db.py`` clears ``claim_lock`` inside the
   same ``write_txn`` that moves the status — ``complete_task``, ``block_task``
   (both the dependency branch and the recurrence-limit/triage branch), the
   unblock recovery path, ``archive_task``, ``schedule_task``, and both
   reclaimers — and ``claim_task`` does the mirror image: it flips the status
   to ``running``, writes ``claim_lock``, ``INSERT``s the ``task_runs`` row with
   ``status='running'`` and sets ``tasks.current_run_id`` in one transaction.
   Both claim entry points (the ``ready`` claim and the ``review`` re-claim) are
   built that way, and ``claim_task`` additionally closes a leaked prior run as
   ``reclaimed`` before it re-points ``current_run_id``. ``claim_review_task``
   has no such recovery, so a ``review`` card that somehow still held an open
   run would surface here as an orphan — correctly. So a row that breaks either
   pairing is not a race seen through a ``mode=ro`` WAL snapshot — SQLite hands
   a read-only reader whole transactions — it is real corruption.

   In practice it is the signature of a direct sqlite write against
   ``/opt/data/kanban.db`` from an agent shell, which the persona files forbid
   and nothing enforces: the approval layer's ``approvals.deny`` globs match
   command strings, ``execute_code`` never consults them, and file indirection
   (``sqlite3 db < /tmp/fix.sql``) walks straight past them. These two queries
   are the honest replacement for an enforcement rule that cannot exist —
   detection after the fact, with no false positives to mute.

   One caveat recorded so it is not mistaken for a bug in the query: the
   timeout/crash branch of the failure-limit breaker flips ``running``/``ready``
   to ``blocked`` *without* touching ``claim_lock``, on the documented contract
   that the reclaimer has already cleared it. If that contract is ever broken
   the ghost-claim query is what will say so, which is the point.

2. **Whatever the shipped rule engine says**, obtained by shelling out to
   ``hermes kanban diagnostics --json``. Deliberately not reimplemented here.
   Those rules key off event history (the last ready-transition event, the last
   ``blocked`` event, the configured ``failure_limit``) and carry thresholds
   their authors chose. A hand-written ``WHERE status='ready' AND
   now - created_at > 900`` is not the same query: ``started_at`` freezes at the
   first claim and ``recompute_ready`` promotes without touching a timestamp, so
   it fires on promotions, retries, reclaims, per-profile caps and cards a
   respawn guard is deferring for up to 24h. The rules live in the image; keep
   them there and let the CLI be the contract.

   (2) is NOT read-only, and the distinction matters enough to state: the CLI
   goes through ``kanban_db.connect()``, and in a freshly spawned process
   ``_INITIALIZED_PATHS`` is empty, so every invocation takes the slow path —
   ``preflight_db_writability()`` (which will ``chmod u+rw`` the db and its
   sidecars), ``PRAGMA integrity_check``, then ``executescript(SCHEMA_SQL)`` and
   ``_migrate_add_optional_columns()`` under the bounded ``<db>.init.lock`` —
   before its first ``SELECT``. That is Hermes' own supported entry point, the
   same one ``hermes kanban show`` uses, and it mutates no task or run row; the
   read-write open is the price of not reimplementing the rule engine. Only (1)
   is a ``mode=ro`` handle. The CLI is also pinned to the same board file the
   invariants were read from (see ``diagnostics_lines``), because it would
   otherwise resolve the board independently and the two halves could report on
   different databases.

Deliberately NOT checked:

- *"the dispatcher stopped ticking."* There is no board-side predicate for it
  that does not false-positive: ``dispatch_once`` legitimately leaves cards in
  ``ready`` with a NULL ``claim_lock`` — and emits no event — for the global
  ``max_in_progress`` cap, the per-profile cap, an unassigned card, a
  non-spawnable assignee, and a respawn guard of up to 86400s. The dispatcher
  *does* log its failures (``kanban dispatcher: tick failed on board <slug>``,
  with a traceback, from ``gateway/kanban_watchers_dispatcher.py``) and
  survives them, so a log or OTel alert on that line is the right vehicle and
  belongs to observability. Do not add a query for this.
- *stale ``running`` cards.* ``detect_stale_running`` already reclaims them:
  ``kanban.dispatch_stale_timeout_seconds`` is configured to 1800 (30m) in
  the chat profile and operator manifests (#1880, bounding stale running workers
  from upstream's 14400s / 4h default), and ``release_stale_claims`` reclaims
  expired claims unconditionally. Alerting on a state the harness is about to
  self-heal is how an alert gets muted.
- *cards stranded in ``ready``, stuck in ``blocked``, or failing repeatedly.*
  All three are strictly worse restatements of ``_rule_stranded_in_ready``,
  ``_rule_stuck_in_blocked`` and ``_rule_repeated_failures``, which are
  event-keyed and config-derived. They arrive through (2) or not at all — and
  ``stuck_in_blocked`` always does, floor or no floor (see below).

Scheduled since #656, as ``kanban-board-health`` on the Platform Agent's roster
(``agents/platform/cron/jobs.json``, ``deliver: "chat"``, daily). It shipped
unscheduled because the roster it belonged on could not carry its output to a
human: the Chat Agent's jobs deliver ``local``, which the scheduler resolves to
no target, and the Platform Agent's profile had no board of its own. Two things
changed. ``deliver: "chat"`` now relays a job's stdout into the home channel (``docs/designs/cron-report-relay.md``), and the board
was never per-profile: it is the agent home's ``kanban.db``, which a
platform-roster job reaches through ``PLATFORM_AGENT_HOME``
(``gitops_workspace.agent_home``), not ``$HERMES_HOME``, which under that roster
is ``profiles/platform`` and holds no board. What forced the change was a
triage card that sat ``blocked`` with ``result = NULL`` for weeks because
nothing periodically asked (#656).

``stuck_in_blocked`` is therefore reported whatever the severity floor says: a
worker or operator block is sticky, nothing retries it until a human acts, and
a card nobody is looking at is the one finding this check exists for.

The stuck cards are said in plain words, for whoever is in the channel: a
headline counting them ("2 tasks are stuck and need someone to decide."), then
one row per task, newest first, with the task's own title in bold and how long
it has waited ("Stopped 3 days ago before finishing.", or "Paused 6 days ago."
for a block waiting on a person's answer). No task ids, block reasons or
commands: those stay behind ``hermes kanban list`` and ``hermes kanban show``.
Tasks sharing a title and whether they wait on a person are one row with
their count, and past ``MAX_STUCK_ROWS`` rows the rest are counted.

They are said when a task first gets stuck, then once a week while any stays
stuck (``stuck_due``); a run in between says nothing about them. The record of
the ids last said, and when, is ``profiles/platform/cron/kanban_board_health.json``
under the agent home (``STATE_ENV`` overrides). A record that cannot be read
says every stuck task again, which is the loud side to fail on. The other
findings (invariant violations, the engine's other rules) are rare and stay
stateless, under ``REPORT_HEADER``.

Run on demand:  PLATFORM_AGENT_HOME=/opt/data python3 kanban_board_health.py
(a hand run updates the record; point ``KANBAN_HEALTH_STATE`` elsewhere to keep it)
"""

import collections
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# A sibling in the shared scripts directory: the agent home is `PLATFORM_AGENT_HOME`,
# never `HERMES_HOME`, which under the platform roster names the profile home.
import gitops_workspace

# Severity floor applied to what `hermes kanban diagnostics` returns. "error" on
# purpose: the engine's warning-tier rules are useful to pull on demand but too
# chatty to push into chat daily, with the one exception named below. Override
# with KANBAN_HEALTH_SEVERITY.
DEFAULT_SEVERITY = "error"
SEVERITY_ENV = "KANBAN_HEALTH_SEVERITY"
SEVERITIES = ("warning", "error", "critical")
# Rule kinds reported whatever the floor says. `stuck_in_blocked` is a warning
# in the engine, and it is the finding #656 was about: a sticky block that no
# retry will ever clear, waiting for a human nobody has told.
ALWAYS_REPORT_KINDS = frozenset({"stuck_in_blocked"})
ALWAYS_REPORT_ENV = "KANBAN_HEALTH_ALWAYS_REPORT"
STUCK_IN_BLOCKED = "stuck_in_blocked"
# The engine is asked for everything and filtered here, so the always-report
# kinds reach this script whatever the floor.
ENGINE_SEVERITY = SEVERITIES[0]
# Where the board lives, for tests and hand runs; otherwise the agent home.
HOME_ENV = "KANBAN_HEALTH_HOME"
# Rendering. The engine's `detail` runs to several paragraphs; chat gets its
# first line, clipped, and the rest stays behind `hermes kanban diagnostics`.
DETAIL_MAX_CHARS = 200
MAX_DIAGNOSTIC_ITEMS = 20
MAX_DIAGNOSTIC_CHARS = 3500
MAX_INVARIANT_ITEMS = 20
MAX_REPORT_CHARS = 8500
# The other findings' header, under the stuck tasks when both are present.
REPORT_HEADER = "kanban board health: attention required"
# What the header and its newline take of the other findings' budget.
REPORT_HEADER_RESERVE = len(REPORT_HEADER) + 1
# The room an omission line ("... and N other ... omitted") needs; below it
# the line is left off rather than pushing the report past its budget.
OMISSION_LINE_RESERVE = 120

# The stuck tasks, in plain words: the task's own title and how long it has
# waited, no ids, reasons or commands. Standard markdown, which the relay's
# chat adapters (and the gateway under `spec.mode: next`) turn into each
# platform's bold.
STUCK_HEADLINE = (
    "**{count} task is stuck and needs someone to decide.**",
    "**{count} tasks are stuck and need someone to decide.**",
)
STUCK_ROW_TITLE = "**{title}**"
STUCK_GROUP_SUFFIX = " ({count} tasks)"
UNTITLED_TASK = "Untitled task"
# A block that waits on a person reads "Paused"; every other kind is a task
# that stopped before it finished.
PERSON_BLOCK_KINDS = frozenset({"needs_input"})
PAUSED_LINE = "Paused {age} ago."
PAUSED_LINE_NO_AGE = "Paused."
STOPPED_LINE = "Stopped {age} ago before finishing."
STOPPED_LINE_NO_AGE = "Stopped before finishing."
# The todo cards a stuck task holds back (a child waits until every parent is
# done), by title: the first, cut shorter than a row's title, then a count of
# the rest.
HELD_LINE = "Holding back: {titles}."
HELD_MORE = " and {count} more"
HELD_TITLES_MAX = 1
HELD_TITLE_MAX_CHARS = 80
HELD_SEPARATOR = ", "
AGE_UNITS = (("day", "days"), ("hour", "hours"))
HOURS_PER_DAY = 24
STUCK_MORE_LINE = "And {count} more."
# Rows past this are counted, not listed; a title is cut at a word boundary.
MAX_STUCK_ROWS = 15
STUCK_TITLE_MAX_CHARS = 120
# What the stuck tasks can take of `MAX_REPORT_CHARS`: the headline, the
# rows' bold titles with a group count, their status lines, and the count of
# the rest, with room to spare.
STUCK_CARD_RESERVE = 5000
ELLIPSIS = "…"
# A word cut keeping less than 1/CLIP_MIN_SHARE of the budget falls back to the
# hard cut: a long unbroken token would otherwise take the rest of the title with
# it (slack_status._clip carries the same guard).
CLIP_MIN_SHARE = 2
# Markdown emphasis inside a title would close the row's bold early.
TITLE_EMPHASIS = "*"

# Stuck tasks are said when one first gets stuck and then once a week while
# any stays stuck; a run in between prints nothing about them. The record of
# what was said lives beside the other platform watches' ledgers.
REMIND_AFTER_DAYS = 7
SECONDS_PER_HOUR = 3600
# The run that posted may have been a catch-up hours after 12:35 UTC (the
# scheduler fires a missed daily job once, whenever the gateway is back), so a
# week measured from it would slip the reminder to the eighth day. Half a day
# early still counts: a once-a-day job cannot fire twice inside that.
REMIND_SLACK_SECONDS = 12 * SECONDS_PER_HOUR
STATE_ENV = "KANBAN_HEALTH_STATE"
PROFILES_DIR = "profiles"
PLATFORM_PROFILE = "platform"
CRON_DIR = "cron"
STATE_FILE_NAME = "kanban_board_health.json"
# The platform store the scheduler stamps after delivery, and this job's entry
# in it (agents/platform/cron/jobs.json).
JOBS_FILE = "jobs.json"
JOB_ID = "kanban-board-health"
STATE_TMP_SUFFIX = ".tmp"
STATE_SCHEMA_VERSION = 1
STATE_VERSION_KEY = "version"
STATE_SAID_KEY = "said"
STATE_SAID_AT_KEY = "said_at"

# The line a run where the engine did not answer returns, and how `report`
# tells that run from a clean one: its stuck cards are unknown, not absent.
DIAGNOSTICS_UNAVAILABLE = "  diagnostics unavailable"

# The CLI opens the board through Hermes' own connection and may run an
# idempotent migration; 60s is generous for a board of this size and still well
# inside a cron slot.
DIAGNOSTICS_TIMEOUT_SECONDS = 60

# A run row claiming to be alive that its task does not point at. claim_task
# writes the INSERT, current_run_id and status='running' in one write_txn, and
# every terminal path calls _end_run in the txn that moves the status — so this
# cannot be a snapshot artefact.
ORPHAN_RUNS_SQL = """
    SELECT r.id AS run_id, r.task_id, t.status AS task_status
      FROM task_runs r
      JOIN tasks t ON t.id = r.task_id
     WHERE r.status = 'running'
       AND (t.status <> 'running'
            OR t.current_run_id IS NULL
            OR t.current_run_id <> r.id)
     ORDER BY r.id
"""

# A claim held by a card that is not running. Every non-running transition sets
# claim_lock = NULL in the same statement that sets the status.
GHOST_CLAIMS_SQL = """
    SELECT id, status, claim_lock
      FROM tasks
     WHERE claim_lock IS NOT NULL
       AND status <> 'running'
     ORDER BY id
"""

# What the engine's JSON does not carry about a blocked card: its title, and
# its kind as the last `blocked` event recorded it (`kanban_db.block_task`
# writes `{"reason": ..., "kind": ...}` as the event payload).
BLOCKED_TASKS_SQL = """
    SELECT id, title, block_kind
      FROM tasks
     WHERE status = 'blocked'
"""
BLOCKED_EVENTS_SQL = """
    SELECT task_id, payload
      FROM task_events
     WHERE kind = 'blocked'
     ORDER BY created_at, id
"""
BLOCKED_CHILDREN_SQL = """
    SELECT l.parent_id, c.id, c.title, c.assignee
      FROM task_links l
      JOIN tasks c ON c.id = l.child_id
     WHERE c.status = 'todo'
     ORDER BY c.id
"""
# The words `session_kv_server._triage_authorize_clauses(True)` puts in a
# triage report's `To authorize:` bullet when the install opens the Pull
# Request itself (`TRIAGE_OPEN_PR_MARKER` there). The Slack notifier
# (`slack_ux_incident.OPENING_PULL_REQUEST`) and the bench case key on the same
# words, so all three readers agree on which reports made the promise.
TRIAGE_OPEN_PR_MARKER = "is being opened automatically"
# A completed triage card whose report made that promise must have its linked
# child card in `task_links`. If the front-door turn ended between Call 1 and
# Call 2, the reader sees a report telling them not to reply `apply` while no
# follow-up card exists to open the Pull Request.
MISSING_PR_CHILD_SQL = f"""
    SELECT t.id, t.title
      FROM tasks t
     WHERE t.status = 'done'
       AND t.title LIKE 'Triage %'
       AND t.result LIKE '%{TRIAGE_OPEN_PR_MARKER}%'
       AND NOT EXISTS (
           SELECT 1 FROM task_links l WHERE l.parent_id = t.id
       )
     ORDER BY t.id
"""


def agent_home() -> Path:
    """The volume root that holds `kanban.db`.

    Not `HERMES_HOME`: under the platform roster that is `profiles/platform`, a
    directory with no board. `HOME_ENV` overrides for tests and hand runs.
    """
    return Path(os.environ.get(HOME_ENV) or gitops_workspace.agent_home())


def board_path(home: Path) -> Path:
    return home / "kanban.db"


def hermes_bin() -> Path:
    """Locate ``hermes``, preferring the one beside the running interpreter.

    Same resolution as profile_cron_tick.py: the scheduler launches a
    ``no_agent`` script with ``sys.executable``, which is the gateway's own venv
    interpreter, so its sibling ``hermes`` is by construction the same install
    the gateway runs. PATH is the fallback for a dev checkout.
    """
    sibling = Path(sys.executable).with_name("hermes")
    if sibling.is_file():
        return sibling
    found = shutil.which("hermes")
    if found:
        return Path(found)
    raise FileNotFoundError(f"no `hermes` beside {sys.executable} or on PATH")


def read_only_connection(db: Path) -> sqlite3.Connection:
    """Open the board read-only. Never relax *this* connection.

    The gateway is actively writing this WAL database. A read-write handle on
    the invariant queries would make them the direct-write vector they exist to
    detect, and ``query_only`` makes that a hard error rather than a code
    review: the ``mode=ro`` URI refuses to create the file and the pragma
    refuses the statement. (The diagnostics shell-out is a separate matter and
    does open read-write, through Hermes' own connector — see item (2) of the
    module docstring.)

    Measured caveat, so nobody reads "read-only" as "touches no inode": the
    board is in WAL mode, and any reader — this one, ``hermes kanban show``,
    the dashboard — materialises the ``-shm`` index and a zero-length ``-wal``
    if the last writer checkpointed them away. The database file itself is
    byte-identical across an open (verified by size, sha256 and mtime).
    """
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def blocked_context(conn: sqlite3.Connection) -> dict:
    """Title, kind and the todo children it holds back, per blocked card, or {} on an unmigrated board."""
    try:
        tasks = {
            row["id"]: {"title": row["title"] or "", "kind": row["block_kind"]}
            for row in conn.execute(BLOCKED_TASKS_SQL)
        }
        for row in conn.execute(BLOCKED_EVENTS_SQL):
            if row["task_id"] not in tasks:
                continue
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            if isinstance(payload, dict):
                # Events arrive oldest first, so the last kind wins.
                if payload.get("kind"):
                    tasks[row["task_id"]]["kind"] = payload["kind"]
        try:
            for row in conn.execute(BLOCKED_CHILDREN_SQL):
                if row["parent_id"] not in tasks:
                    continue
                tasks[row["parent_id"]].setdefault("waiting_children", []).append({
                    "id": row["id"],
                    "title": row["title"] or "",
                    "assignee": row["assignee"] or "?",
                })
        except sqlite3.OperationalError:
            pass
    except sqlite3.OperationalError:
        return {}
    return tasks


def first_line(text: str, limit: int) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def _card_age_hours(diag: dict) -> float:
    try:
        val = (diag.get("data") or {}).get("age_hours")
        if val is not None:
            return float(val)
    except (TypeError, ValueError, AttributeError):
        pass
    return float("inf")


def _take_within_budget(items: list, max_items: int, max_chars: int, line_fn=None) -> list:
    """Take items up to max_items and max_chars (including newlines)."""
    taken = []
    total_chars = 0
    for item in items:
        if len(taken) >= max_items:
            break
        lines = line_fn(item) if line_fn else item
        if isinstance(lines, str):
            lines = [lines]
        item_chars = sum(len(ln) + 1 for ln in lines)
        if total_chars + item_chars > max_chars:
            continue
        taken.append(item)
        total_chars += item_chars
    return taken


def stuck_tasks(stuck_items: list, context: dict) -> list:
    """One dict per stuck card: id, title, whether it waits on a person, and its age.

    The board's title and kind first (``blocked_context``), the CLI's title as
    the fallback, and the titles of the todo cards it holds back; the reason is
    not carried, since chat never shows it.
    """
    tasks = []
    for task_id, diag, entry in stuck_items:
        task_id = str(task_id)
        info = (context or {}).get(task_id) or {}
        raw_title = info.get("title") or (entry or {}).get("title") or ""
        tasks.append({
            "task_id": task_id,
            "title": " ".join(str(raw_title).split()),
            "person": info.get("kind") in PERSON_BLOCK_KINDS,
            "age_hours": _card_age_hours(diag),
            "held": [str(child.get("title") or "") for child in info.get("waiting_children") or ()],
        })
    return tasks


def plain_age(age_hours: float) -> str | None:
    """"3 days" or "5 hours"; None when the engine gave no age, or one that is not a number."""
    if not math.isfinite(age_hours):
        return None
    hours = max(0, int(age_hours))
    days = hours // HOURS_PER_DAY
    count, units = (days, AGE_UNITS[0]) if days else (hours, AGE_UNITS[1])
    return f"{count} {units[0] if count == 1 else units[1]}"


def plain_title(title: str, limit: int = STUCK_TITLE_MAX_CHARS) -> str:
    """The task's title without emphasis markers, cut at a word boundary within ``limit``."""
    text = " ".join(title.replace(TITLE_EMPHASIS, "").split()) or UNTITLED_TASK
    if len(text) <= limit:
        return text
    cut = text[: limit - len(ELLIPSIS)]
    head, space, _rest = cut.rpartition(" ")
    if not space or len(head) * CLIP_MIN_SHARE < len(cut):
        head = cut
    return head.rstrip() + ELLIPSIS


def held_line(group: list) -> str | None:
    """"Holding back: <title> and N more." for the todo cards a row's tasks hold back, or None."""
    titles = list(dict.fromkeys(plain_title(title, HELD_TITLE_MAX_CHARS) for task in group for title in task.get("held") or ()))
    if not titles:
        return None
    shown = HELD_SEPARATOR.join(titles[:HELD_TITLES_MAX])
    rest = len(titles) - HELD_TITLES_MAX
    return HELD_LINE.format(titles=shown + (HELD_MORE.format(count=rest) if rest > 0 else ""))


def status_line(person: bool, age_hours: float) -> str:
    age = plain_age(age_hours)
    if person:
        return PAUSED_LINE.format(age=age) if age else PAUSED_LINE_NO_AGE
    return STOPPED_LINE.format(age=age) if age else STOPPED_LINE_NO_AGE


def render_stuck_tasks(tasks: list) -> list:
    """The stuck tasks as chat lines: a headline counting every task, then a row per title.

    Rows are newest first. Tasks sharing a title as shown (``plain_title``)
    and whether they wait on a person are one row with their count, aged by the
    one that has waited longest, so two rows never read the same. Past
    ``MAX_STUCK_ROWS`` the rest are counted. Empty for no tasks.
    """
    if not tasks:
        return []
    groups = collections.OrderedDict()
    for task in tasks:
        groups.setdefault((plain_title(task["title"]), task["person"]), []).append(task)
    rows = sorted(
        groups.values(),
        key=lambda group: (min(t["age_hours"] for t in group), group[0]["title"]),
    )
    noun = STUCK_HEADLINE[0] if len(tasks) == 1 else STUCK_HEADLINE[1]
    lines = [noun.format(count=len(tasks))]
    for group in rows[:MAX_STUCK_ROWS]:
        title = STUCK_ROW_TITLE.format(title=plain_title(group[0]["title"]))
        if len(group) > 1:
            title += STUCK_GROUP_SUFFIX.format(count=len(group))
        oldest = max(t["age_hours"] for t in group)
        lines.extend(["", title, status_line(group[0]["person"], oldest)])
        held = held_line(group)
        if held:
            lines.append(held)
    hidden = sum(len(group) for group in rows[MAX_STUCK_ROWS:])
    if hidden:
        lines.extend(["", STUCK_MORE_LINE.format(count=hidden)])
    return lines


def state_path(home: Path, env=None) -> Path:
    """Where the record of what was said lives: `STATE_ENV`, else beside the platform watches' ledgers."""
    override = (env if env is not None else os.environ).get(STATE_ENV)
    return Path(override) if override else home / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / STATE_FILE_NAME


def load_state(path: Path) -> dict:
    """The ids last said and when; empty (so the next stuck task is said) when missing or unreadable.

    Empty is the same record ``stuck_due`` keeps for a board with nothing
    stuck, so a healthy run writes nothing.
    """
    empty = {STATE_SAID_KEY: [], STATE_SAID_AT_KEY: None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or data.get(STATE_VERSION_KEY) != STATE_SCHEMA_VERSION:
        return empty
    said = data.get(STATE_SAID_KEY)
    said_at = data.get(STATE_SAID_AT_KEY)
    return {
        STATE_SAID_KEY: [str(tid) for tid in said] if isinstance(said, list) else [],
        STATE_SAID_AT_KEY: said_at if isinstance(said_at, (int, float)) and math.isfinite(said_at) else None,
    }


def last_post_undelivered(home: Path, said_at, env=None) -> bool:
    """Whether the run that last said the stuck tasks failed to deliver them.

    The record is saved before the relay delivers, so it cannot know. The
    scheduler can: after delivery it stamps this job's entry in the platform
    store with ``last_run_at`` and, on a failure, ``last_delivery_error``. An
    error stamped at or after ``said_at`` belongs to that run (or a later one),
    so the tasks are said again rather than left unreported for a week. Under
    ``STATE_ENV`` (tests and hand runs) the store is not read. A failure grade
    can also mark a post that landed (``feedback_prompt.py`` says why), which
    costs a repeat card the next day, not a silent week.
    """
    if said_at is None or (env if env is not None else os.environ).get(STATE_ENV):
        return False
    try:
        data = json.loads((home / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / JOBS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == JOB_ID), None) if isinstance(jobs, list) else None
    if not job or not job.get("last_delivery_error"):
        return False
    try:
        run_at = datetime.fromisoformat(str(job.get("last_run_at")).replace("Z", "+00:00"))
    except ValueError:
        return False
    if run_at.tzinfo is None:
        run_at = run_at.replace(tzinfo=timezone.utc)
    return run_at.timestamp() >= said_at


def save_state(path: Path, state: dict) -> None:
    """Write the record atomically. A record that cannot be written leaves the
    tasks said again on the next run, since nothing can remember them."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + STATE_TMP_SUFFIX)
        tmp.write_text(json.dumps({STATE_VERSION_KEY: STATE_SCHEMA_VERSION, **state}, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def stuck_due(tasks: list, state: dict, now: float) -> tuple[bool, dict]:
    """Whether to say the stuck tasks this run, and the record to keep.

    Said when a task is stuck that the last post did not name, or when
    ``REMIND_AFTER_DAYS`` have passed since the last post (less
    ``REMIND_SLACK_SECONDS``) and any task is still stuck. A task that comes
    unstuck drops out of the record, so if it sticks again it is new. A
    ``said_at`` ahead of ``now`` (a clock step, a hand-edited record) is treated
    as no post at all, so it cannot silence the tasks until it ages a week.
    ``now`` is epoch seconds.
    """
    current = sorted({task["task_id"] for task in tasks})
    said = set(state.get(STATE_SAID_KEY) or [])
    said_at = state.get(STATE_SAID_AT_KEY)
    if said_at is not None and said_at > now:
        said_at = None
    if not current:
        return False, {STATE_SAID_KEY: [], STATE_SAID_AT_KEY: None}
    remind_after = REMIND_AFTER_DAYS * HOURS_PER_DAY * SECONDS_PER_HOUR - REMIND_SLACK_SECONDS
    if set(current) - said or said_at is None or now - said_at >= remind_after:
        return True, {STATE_SAID_KEY: current, STATE_SAID_AT_KEY: now}
    return False, {STATE_SAID_KEY: sorted(said & set(current)), STATE_SAID_AT_KEY: said_at}


def invariant_violations(conn: sqlite3.Connection) -> list:
    """Rows that no code path in kanban_db.py can produce. Empty when healthy."""
    lines = []
    for row in conn.execute(ORPHAN_RUNS_SQL):
        lines.append(
            f"  orphan run: task_runs.id={row['run_id']} is 'running', but task "
            f"{row['task_id']} is '{row['task_status']}' or does not point at it"
        )
    for row in conn.execute(GHOST_CLAIMS_SQL):
        lines.append(
            f"  ghost claim: task {row['id']} is '{row['status']}' but still holds "
            f"claim_lock={row['claim_lock']!r}"
        )
    try:
        for row in conn.execute(MISSING_PR_CHILD_SQL):
            lines.append(
                f"  missing follow-up child: task {row['id']} ({row['title']!r}) promised "
                "an automatic Pull Request but has no linked child in task_links"
            )
    except sqlite3.OperationalError:
        pass
    return lines


def severity_floor(env=None) -> str:
    value = ((env if env is not None else os.environ).get(SEVERITY_ENV) or "").strip().lower()
    return value if value in SEVERITIES else DEFAULT_SEVERITY


def always_report_kinds(env=None) -> frozenset:
    raw = ((env if env is not None else os.environ).get(ALWAYS_REPORT_ENV) or "").strip()
    if not raw:
        return ALWAYS_REPORT_KINDS
    return frozenset(k.strip() for k in raw.split(",") if k.strip())


def clears_floor(severity: str, floor: str) -> bool:
    known = severity in SEVERITIES
    return not known or SEVERITIES.index(severity) >= SEVERITIES.index(floor)


def _default_runner(argv, env, timeout):
    proc = subprocess.run(
        argv, env=env, timeout=timeout, capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_diagnostics_json(out: str):
    """Tolerant parse: the CLI may prefix stdout with a startup log line."""
    text = (out or "").strip()
    if not text:
        return []
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        if start < 0:
            raise
        return json.loads(text[start:])


def diagnostics_lines(home: Path, severity: str, runner=_default_runner,
                      binary=None, env=None, always=ALWAYS_REPORT_KINDS,
                      *, stuck: list | None = None, max_chars: int | None = None) -> list:
    """Ask the shipped rule engine. Returns rendered lines; empty when clean.

    ``--json`` returns before the human-table branch of ``_cmd_diagnostics``, so
    a clean board prints ``[]`` rather than "No active diagnostics on this
    board." — which is why nothing here has to pattern-match prose.

    The engine is asked for every severity and the floor is applied here, so a
    kind in ``always`` (``stuck_in_blocked`` by default) is reported however low
    the engine grades it. Stuck cards are not rendered here: each is appended
    to ``stuck`` as ``(task_id, diag, entry)`` for ``stuck_tasks``, and a call
    without the list drops them. Everything else keeps the engine's own title
    and detail, within max_chars. A run where the engine did not answer
    returns one line starting ``DIAGNOSTICS_UNAVAILABLE``.
    """
    try:
        exe = str(binary) if binary else str(hermes_bin())
    except FileNotFoundError as exc:
        return [f"{DIAGNOSTICS_UNAVAILABLE}: {exc}"]
    child_env = dict(env if env is not None else os.environ)
    child_env["HERMES_HOME"] = str(home)
    # Pin the engine to the file the invariant queries just read. Left alone,
    # `kanban_db_path()` resolves the board itself — inherited HERMES_KANBAN_DB
    # first (the dispatcher injects it into worker env), then
    # `<root>/kanban/current`, mapping a non-default slug to
    # `<root>/kanban/boards/<slug>/kanban.db`. The two halves of this check
    # would then be reporting on different databases. HERMES_KANBAN_DB has the
    # highest precedence, so setting it also neutralises anything inherited.
    child_env["HERMES_KANBAN_DB"] = str(board_path(home))
    argv = [exe, "kanban", "diagnostics", "--json", "--severity", ENGINE_SEVERITY]
    try:
        code, out, err = runner(argv, child_env, DIAGNOSTICS_TIMEOUT_SECONDS)
    except Exception as exc:  # timeout, OSError, anything the runner raises
        return [f"{DIAGNOSTICS_UNAVAILABLE}: {type(exc).__name__}: {exc}"]
    if code != 0:
        detail = [ln for ln in ((err or out or "").strip().splitlines()) if ln.strip()]
        tail = detail[-1] if detail else f"exit {code}"
        return [f"{DIAGNOSTICS_UNAVAILABLE}: `hermes kanban diagnostics` exit {code}: {tail}"]
    try:
        entries = _parse_diagnostics_json(out)
    except json.JSONDecodeError as exc:
        return [f"{DIAGNOSTICS_UNAVAILABLE}: unparseable JSON from the CLI ({exc})"]
    # The `--json` branch of `_cmd_diagnostics` always emits a list, so this is
    # belt-and-braces — but the contract of this whole function is that it
    # returns lines and never raises, and a dict or a bare int would otherwise
    # escape as a traceback that the scheduler reports as a failed watchdog.
    if entries is not None and not isinstance(entries, list):
        return [f"{DIAGNOSTICS_UNAVAILABLE}: CLI returned "
                f"{type(entries).__name__}, expected a JSON list"]
    if max_chars is None:
        max_chars = MAX_REPORT_CHARS

    diag_lines = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("task_id") or "?")
        status = str(entry.get("status") or "?")
        for diag in entry.get("diagnostics") or []:
            if not isinstance(diag, dict):
                continue
            kind = diag.get("kind")
            if kind == STUCK_IN_BLOCKED and kind in always:
                if stuck is not None:
                    stuck.append((task_id, diag, entry))
                continue
            if not clears_floor(str(diag.get("severity", "")), severity) and kind not in always:
                continue
            parts = [f"  [{diag.get('severity', '?')}] {task_id} ({status}): "
                     f"{diag.get('title', diag.get('kind', 'diagnostic'))}"]
            detail = first_line((diag.get("detail") or ""), DETAIL_MAX_CHARS)
            if detail:
                parts.append(f" - {detail}")
            diag_lines.append("".join(parts))

    diag_budget = max(0, min(MAX_DIAGNOSTIC_CHARS, max_chars) - OMISSION_LINE_RESERVE)
    shown_diag_lines = _take_within_budget(diag_lines, MAX_DIAGNOSTIC_ITEMS, diag_budget)
    lines = list(shown_diag_lines)
    omitted_diags = len(diag_lines) - len(shown_diag_lines)
    if omitted_diags > 0 and max_chars >= OMISSION_LINE_RESERVE:
        lines.append(
            f"  ... and {omitted_diags} other diagnostic{'s' if omitted_diags != 1 else ''} omitted "
            f"(run hermes kanban diagnostics on the agent pod to view all)"
        )
    return lines


def report(home: Path, *, env=None, max_chars: int | None = None, **kwargs) -> tuple[list | None, list]:
    """The whole check: ``(stuck tasks, other lines)``. Both empty means healthy, which means silent.

    The stuck tasks are ``stuck_tasks`` dicts, which ``main`` renders when
    ``stuck_due`` says so, or None when the engine did not answer (its line is
    among the other lines), since then which cards are stuck is unknown. The other lines (invariant violations and the
    engine's other findings) stay <= max_chars (including
    ``REPORT_HEADER_RESERVE``); the default leaves ``STUCK_CARD_RESERVE`` of the
    report's budget to the stuck tasks.
    """
    if max_chars is None:
        max_chars = MAX_REPORT_CHARS - STUCK_CARD_RESERVE
    db = board_path(home)
    if not db.exists():
        # No board on this profile: not a failure, nothing to say. Skipping the
        # shell-out here is deliberate — the CLI would create one.
        return [], []
    lines = []
    conn = read_only_connection(db)
    try:
        violations = invariant_violations(conn)
        context = blocked_context(conn)
    finally:
        conn.close()

    used_chars = REPORT_HEADER_RESERVE
    if violations:
        header = "Board invariant violations (a direct write to kanban.db is the usual cause):"
        lines.append(header)
        header_len = len(header) + 1
        used_chars += header_len
        viol_allowance = max(0, max_chars - used_chars)
        viol_budget = max(0, min(MAX_DIAGNOSTIC_CHARS, viol_allowance) - 120)
        shown_violations = _take_within_budget(violations, MAX_INVARIANT_ITEMS, viol_budget)
        lines.extend(shown_violations)
        omitted_violations = len(violations) - len(shown_violations)
        if omitted_violations > 0 and viol_allowance >= 120:
            lines.append(
                f"  ... and {omitted_violations} other invariant violation{'s' if omitted_violations != 1 else ''} omitted"
            )
        used_chars = REPORT_HEADER_RESERVE + sum(len(ln) + 1 for ln in lines)

    diag_header_reserve = len("Active kanban diagnostics:\n")
    remaining_chars = max(0, max_chars - used_chars - diag_header_reserve)
    stuck = []
    diags = diagnostics_lines(
        home, severity_floor(env), env=env, always=always_report_kinds(env),
        stuck=stuck, max_chars=remaining_chars, **kwargs
    )
    if diags:
        lines.append("Active kanban diagnostics:")
        lines.extend(diags)
    if any(line.startswith(DIAGNOSTICS_UNAVAILABLE) for line in diags):
        return None, lines
    return stuck_tasks(stuck, context), lines


def main(argv=None, *, env=None, now=None) -> int:
    home = agent_home()
    path = state_path(home, env)
    recorded = load_state(path)
    # The scheduler's stamp of a failed delivery lasts one run, so it is read
    # on every run, whatever the engine says: a run that could not read the
    # board still forgets the undelivered post, and the next answer says it.
    state = recorded
    if last_post_undelivered(home, recorded[STATE_SAID_AT_KEY], env):
        state = {STATE_SAID_KEY: [], STATE_SAID_AT_KEY: None}
    try:
        tasks, lines = report(home, env=env)
    except sqlite3.Error as exc:
        if state != recorded:
            save_state(path, state)
        print(f"kanban board health: cannot read {board_path(home)}: {exc}")
        return 0
    out = []
    # An engine that did not answer leaves the ids as they were: saving would
    # forget every task said, and the next answer would say them all again.
    if tasks is not None:
        due, state = stuck_due(tasks, state, time.time() if now is None else now)
        # Rendered before the record is saved, so a card that fails to render
        # is not remembered as said.
        out = render_stuck_tasks(tasks) if due else []
    # Compared with what is on disk, so a forgotten post is written down even
    # on a day the new record matches the empty one.
    if state != recorded:
        save_state(path, state)
    if lines:
        out.extend([""] if out else [])
        out.extend([REPORT_HEADER, *lines])
    if out:
        print("\n".join(out))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
