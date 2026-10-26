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
a card nobody is looking at is the one finding this check exists for. The rule is stateless and a card still blocked is named (directly, or by task ID
in the omitted list up to the report budget) once a day until someone unblocks,
comments on, or archives it, which is the intended nag.
Cards are listed newest first, each with its kind, reason, and age read from the
board, so recent outages surface at the top. Where multiple cards share a title,
they are grouped: small groups list each card's task ID, kind, age, and reason,
while larger groups summarize the count, kind breakdown, and oldest age,
followed by the newest card's details and task IDs. Long reports are bounded
to keep recent outage cards and the footer commands within chat limits,
summarizing any omitted cards by task ID up to the character budget (and
falling back to a count and list command when omitted IDs exceed the limit). The
operator unblock and archive commands appear once in a footer (noting they run
on the agent pod) rather than repeated under every card.

Run on demand:  PLATFORM_AGENT_HOME=/opt/data python3 kanban_board_health.py
"""

import collections
import json
import os
import shutil
import sqlite3
import subprocess
import sys
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
REASON_MAX_CHARS = 200
TITLE_MAX_CHARS = 200
HOURS_PER_DAY = 24
AGE_IN_HOURS_BELOW_H = 48
NO_KIND = "no kind"
MAX_STUCK_ITEMS = 15
MAX_STUCK_CHARS = 5500
MAX_DIAGNOSTIC_ITEMS = 20
MAX_DIAGNOSTIC_CHARS = 3500
MAX_INVARIANT_ITEMS = 20
MAX_REPORT_CHARS = 8500
MAX_GROUP_IDS_CHARS = 1000
GROUP_EXPAND_MAX = 3
# Pinned to the board file this run read, the way `diagnostics_lines` pins the
# engine: `kanban_db_path()` would otherwise honour `kanban/current` first.
UNBLOCK_COMMAND = 'HERMES_HOME={home} HERMES_KANBAN_DB={home}/kanban.db hermes kanban unblock --reason "<why>" {task_id}'
ARCHIVE_COMMAND = "HERMES_HOME={home} HERMES_KANBAN_DB={home}/kanban.db hermes kanban archive {task_id}"

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

# What the engine's JSON does not carry about a blocked card: its kind, and
# the reason the last `blocked` event recorded (`kanban_db.block_task` writes
# `{"reason": ..., "kind": ...}` as the event payload).
BLOCKED_TASKS_SQL = """
    SELECT id, title, assignee, block_kind
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
    """kind, reason, title, assignee and waiting todo children per blocked card, or {} on an unmigrated board."""
    try:
        tasks = {
            row["id"]: {"title": row["title"] or "", "assignee": row["assignee"] or "", "kind": row["block_kind"], "reason": ""}
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
                # Events arrive oldest first, so the last one wins for both fields.
                tasks[row["task_id"]]["reason"] = str(payload.get("reason") or tasks[row["task_id"]]["reason"])
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


def render_age(age_hours) -> str:
    try:
        hours = float(age_hours)
    except (TypeError, ValueError):
        return "?"
    if hours < AGE_IN_HOURS_BELOW_H:
        return f"{int(hours)}h"
    return f"{int(hours // HOURS_PER_DAY)}d"


def stuck_card_line(task_id: str, diag: dict, context: dict, entry: dict) -> str:
    """One stuck card: the board's kind and reason, the CLI's title and assignee as fallback."""
    info = (context or {}).get(task_id) or {}
    kind = info.get("kind") or NO_KIND
    assignee = info.get("assignee") or entry.get("assignee") or "?"
    raw_title = info.get("title") or entry.get("title") or ""
    title = " ".join(str(raw_title).split())[:TITLE_MAX_CHARS] if raw_title else ""
    reason = " ".join((info.get("reason") or "").split())[:REASON_MAX_CHARS]
    age = render_age((diag.get("data") or {}).get("age_hours"))
    head = f"  - {task_id} ({assignee}, {kind}, blocked {age})"
    if title:
        head += f": {title}"
    if reason:
        head += f' — "{reason}"'
    return head


def waiting_child_lines(task_id: str, context: dict, indent: str = "    ") -> list:
    """One line per todo child the blocked card holds back (`task_links`).

    A child waits until every parent is done, so a blocked parent also parks
    its children; without this line the board shows the parent and nothing
    says the follow-up card (for instance an incident triage's pull-request
    card) is stuck behind it.
    """
    info = (context or {}).get(task_id) or {}
    lines = []
    for child in info.get("waiting_children") or ():
        line = f"{indent}holding todo child: {child['id']} ({child.get('assignee') or '?'})"
        if child.get("title"):
            line += f": {' '.join(str(child['title']).split())[:TITLE_MAX_CHARS]}"
        lines.append(line)
    return lines


def _card_age_hours(diag: dict) -> float:
    try:
        val = (diag.get("data") or {}).get("age_hours")
        if val is not None:
            return float(val)
    except (TypeError, ValueError, AttributeError):
        pass
    return float("inf")


def _format_id_list_with_dropped(ids: list, max_chars: int = MAX_GROUP_IDS_CHARS) -> tuple[str, list]:
    """Format task IDs comma-separated, truncating with ', and N more' if exceeding max_chars.

    Returns (formatted_str, dropped_ids).
    """
    if not ids:
        return "", []
    all_str = ", ".join(ids)
    if len(all_str) <= max_chars:
        return all_str, []
    chosen = []
    for i, tid in enumerate(ids):
        remaining_count = len(ids) - (i + 1)
        suffix = f", and {remaining_count} more" if remaining_count > 0 else ""
        candidate = (", ".join(chosen + [tid]) + suffix) if chosen else (tid + suffix)
        if len(candidate) > max_chars:
            if chosen:
                break
            return f"and {len(ids)} more", list(ids)
        chosen.append(tid)
    dropped = list(ids[len(chosen):])
    remaining_count = len(dropped)
    if remaining_count > 0:
        return ", ".join(chosen) + f", and {remaining_count} more", dropped
    return ", ".join(chosen), []


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


def _min_stuck_floor(stuck_items: list, home: Path) -> int:
    """Minimum characters render_stuck_cards emits: header + footer + count-only omission line."""
    if not stuck_items:
        return 0
    tid = str(stuck_items[0][0]) if len(stuck_items) == 1 else "<task_id>"
    footer_lines = [
        "  Commands to move a card (run on the agent pod):",
        f"    re-run it:  {UNBLOCK_COMMAND.format(home=home, task_id=tid)}",
        f"    drop it:    {ARCHIVE_COMMAND.format(home=home, task_id=tid)}",
    ]
    footer_chars = sum(len(ln) + 1 for ln in footer_lines)
    header = (
        "Cards blocked with no one looking (a worker or operator block is sticky: "
        "nothing retries it until a human acts):"
    )
    header_chars = len(header) + 1
    omission_sample = (
        f"  ... and {len(stuck_items)} older stuck cards omitted "
        f"(run hermes kanban list on the agent pod to view all)\n"
    )
    return header_chars + footer_chars + len(omission_sample)


def _format_large_group(it: dict, available_chars: int) -> dict | None:
    """Format a large group (>3 cards) with ids: line sized to available_chars."""
    head = it["head"]
    latest_sub = it["latest_sub"]
    group = it["cards"]
    group_ids = it["group_ids"]
    head_chars = len(head) + 1 + len(latest_sub) + 1
    if head_chars > available_chars:
        return None
    lines_for_group = [head, latest_sub]
    rem_for_ids = available_chars - head_chars
    ids_prefix_overhead = len("    ids: ") + 1
    group_ids_budget = min(MAX_GROUP_IDS_CHARS, max(0, rem_for_ids - ids_prefix_overhead))
    formatted_ids, dropped_ids = _format_id_list_with_dropped(group_ids, group_ids_budget)
    if len(dropped_ids) < len(group_ids):
        lines_for_group.append(f"    ids: {formatted_ids}")
    if dropped_ids and len(dropped_ids) == len(group_ids):
        dropped_ids = dropped_ids[1:]
    return {
        "age_hours": it["age_hours"],
        "key": it["key"],
        "lines": lines_for_group,
        "card_count": len(group) - len(dropped_ids),
        "cards": group,
        "dropped_ids": dropped_ids,
        "is_large_group": True,
        "head": head,
        "latest_sub": latest_sub,
        "group_ids": group_ids,
    }


def render_stuck_cards(stuck_items: list, context: dict, home: Path, *, max_chars: int | None = None) -> list:
    """Render stuck cards: grouped by title when duplicated, newest first, commands once.

    Has an irreducible minimum output floor of header + footer + count-only omission line
    (451 + 4*len(str(home)) characters), below which it returns this floor rather than
    truncating operator footer commands or suppressing the omission notice.
    """
    if not stuck_items:
        return []
    if max_chars is None:
        max_chars = MAX_STUCK_CHARS

    tid = str(stuck_items[0][0]) if len(stuck_items) == 1 else "<task_id>"
    footer_lines = [
        "  Commands to move a card (run on the agent pod):",
        f"    re-run it:  {UNBLOCK_COMMAND.format(home=home, task_id=tid)}",
        f"    drop it:    {ARCHIVE_COMMAND.format(home=home, task_id=tid)}",
    ]
    footer_chars = sum(len(ln) + 1 for ln in footer_lines)

    header = (
        "Cards blocked with no one looking (a worker or operator block is sticky: "
        "nothing retries it until a human acts):"
    )
    header_chars = len(header) + 1

    budget_for_body = max(0, max_chars - header_chars - footer_chars)
    min_omission_chars = 120
    available_for_items = max(0, budget_for_body - min_omission_chars)

    parsed = []
    for task_id, diag, entry in stuck_items:
        task_id = str(task_id)
        info = (context or {}).get(task_id) or {}
        kind = str(info.get("kind") or NO_KIND)
        assignee = str(info.get("assignee") or entry.get("assignee") or "?")
        raw_title = info.get("title") or entry.get("title") or ""
        title = " ".join(str(raw_title).split()) if raw_title else ""
        reason = " ".join(str(info.get("reason") or "").split())[:REASON_MAX_CHARS]
        age_hours = _card_age_hours(diag)
        parsed.append({
            "task_id": task_id,
            "diag": diag,
            "entry": entry,
            "kind": kind,
            "assignee": assignee,
            "title": title,
            "reason": reason,
            "age_hours": age_hours,
        })

    by_title = collections.defaultdict(list)
    no_title_cards = []
    for card in parsed:
        if card["title"]:
            by_title[card["title"]].append(card)
        else:
            no_title_cards.append(card)

    renderable_items = []

    for title, group in by_title.items():
        group.sort(key=lambda c: (c["age_hours"], c["task_id"]))
        if len(group) == 1:
            card = group[0]
            line = stuck_card_line(card["task_id"], card["diag"], context, card["entry"])
            renderable_items.append({
                "age_hours": card["age_hours"],
                "key": card["task_id"],
                "lines": [line, *waiting_child_lines(card["task_id"], context)],
                "card_count": 1,
                "cards": [card],
                "dropped_ids": [],
            })
        else:
            newest_age = group[0]["age_hours"]
            oldest_age_hours = max(c["age_hours"] for c in group)
            oldest_str = render_age(oldest_age_hours if oldest_age_hours != float("inf") else None)
            kind_counts = collections.Counter(c["kind"] for c in group)
            sorted_kinds = sorted(kind_counts.items(), key=lambda x: (-x[1], x[0]))
            kinds_summary = ", ".join(f"{cnt} {k}" for k, cnt in sorted_kinds)
            head = f"  - {title[:TITLE_MAX_CHARS]} — {len(group)} cards ({kinds_summary}, oldest {oldest_str})"
            lines_for_group = [head]
            dropped_ids = []
            if len(group) <= GROUP_EXPAND_MAX:
                for card in group:
                    age_str = render_age(card["age_hours"] if card["age_hours"] != float("inf") else None)
                    card_sub = f"    - [{card['task_id']}] ({card['kind']}, {age_str})"
                    if card["reason"]:
                        card_sub += f' — "{card["reason"]}"'
                    lines_for_group.append(card_sub)
                    lines_for_group.extend(waiting_child_lines(card["task_id"], context, indent="      "))
                renderable_items.append({
                    "age_hours": newest_age,
                    "key": title[:TITLE_MAX_CHARS],
                    "lines": lines_for_group,
                    "card_count": len(group),
                    "cards": group,
                    "dropped_ids": [],
                })
            else:
                newest_card = group[0]
                newest_age_str = render_age(newest_card["age_hours"] if newest_card["age_hours"] != float("inf") else None)
                latest_sub = f"    latest: [{newest_card['task_id']}] ({newest_card['kind']}, {newest_age_str})"
                if newest_card["reason"]:
                    latest_sub += f' — "{newest_card["reason"]}"'
                group_ids = [c["task_id"] for c in group]
                renderable_items.append({
                    "age_hours": newest_age,
                    "key": title[:TITLE_MAX_CHARS],
                    "head": head,
                    "latest_sub": latest_sub,
                    "group_ids": group_ids,
                    "cards": group,
                    "is_large_group": True,
                    "lines": [head, latest_sub],
                    "card_count": 1,
                    "dropped_ids": group_ids[1:],
                })

    for card in no_title_cards:
        line = stuck_card_line(card["task_id"], card["diag"], context, card["entry"])
        renderable_items.append({
            "age_hours": card["age_hours"],
            "key": card["task_id"],
            "lines": [line, *waiting_child_lines(card["task_id"], context)],
            "card_count": 1,
            "cards": [card],
            "dropped_ids": [],
        })

    renderable_items.sort(key=lambda item: (item["age_hours"], item["key"]))

    taken_items = []
    total_chars = 0
    taken_origins = set()
    for item in renderable_items:
        if len(taken_items) >= MAX_STUCK_ITEMS:
            break
        remaining_budget = available_for_items - total_chars
        if item.get("is_large_group"):
            formatted = _format_large_group(item, remaining_budget)
            if formatted is None:
                continue
            item_to_take = formatted
            item_chars = sum(len(ln) + 1 for ln in item_to_take["lines"])
        else:
            item_chars = sum(len(ln) + 1 for ln in item["lines"])
            if total_chars + item_chars > available_for_items:
                continue
            item_to_take = item
        taken_items.append(item_to_take)
        taken_origins.add(id(item))
        total_chars += item_chars

    untaken_items = [it for it in renderable_items if id(it) not in taken_origins]

    if len(untaken_items) == 1 and len(taken_items) < MAX_STUCK_ITEMS:
        next_item = untaken_items[0]
        current_chars = sum(sum(len(ln) + 1 for ln in it["lines"]) for it in taken_items)
        remaining_promo_budget = budget_for_body - current_chars
        if not any(it.get("dropped_ids") for it in taken_items):
            if next_item.get("is_large_group"):
                promo_item = _format_large_group(next_item, remaining_promo_budget)
                if promo_item and not promo_item.get("dropped_ids"):
                    taken_items.append(promo_item)
                    untaken_items = []
            else:
                next_chars = sum(len(ln) + 1 for ln in next_item["lines"])
                if not next_item.get("dropped_ids") and (current_chars + next_chars <= budget_for_body):
                    taken_items.append(next_item)
                    untaken_items = []

    taken_items.sort(key=lambda item: (item["age_hours"], item["key"]))

    lines = [header]
    for it in taken_items:
        lines.extend(it["lines"])

    rendered_cards = sum(it["card_count"] for it in taken_items)
    omitted_cards = len(stuck_items) - rendered_cards
    if omitted_cards > 0:
        current_body_chars = sum(len(ln) + 1 for ln in lines[1:])
        remaining_for_omission = max(0, budget_for_body - current_body_chars)
        shown_cards = []
        omitted_cards_list = []
        for it in taken_items:
            shown_cards.extend(it["cards"][:it["card_count"]])
            omitted_cards_list.extend(it["cards"][it["card_count"]:])
        for it in untaken_items:
            omitted_cards_list.extend(it["cards"])

        omitted_cards_sorted = sorted(omitted_cards_list, key=lambda c: (c["age_hours"], c["task_id"]))
        omitted_ids = [c["task_id"] for c in omitted_cards_sorted]

        all_omitted_older = (
            not shown_cards
            or all(o["age_hours"] >= s["age_hours"] for o in omitted_cards_list for s in shown_cards)
        )
        qualifier = "older " if all_omitted_older else "other "
        prefix = f"  ... and {omitted_cards} {qualifier}stuck card{'s' if omitted_cards != 1 else ''} omitted"
        list_hint = "; run hermes kanban list on the agent pod to view all"
        omission_overhead = len(prefix) + len(" (ids: ") + len(list_hint) + len(")\n")
        max_ids_budget = remaining_for_omission - omission_overhead
        if max_ids_budget >= 15:
            formatted_ids, dropped_omitted = _format_id_list_with_dropped(omitted_ids, max_chars=max_ids_budget)
            if len(dropped_omitted) < len(omitted_ids):
                lines.append(f"{prefix} (ids: {formatted_ids}{list_hint})")
            else:
                lines.append(f"{prefix} (run hermes kanban list on the agent pod to view all)")
        else:
            lines.append(f"{prefix} (run hermes kanban list on the agent pod to view all)")

    lines.extend(footer_lines)
    return lines


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
                      binary=None, env=None, context=None, always=ALWAYS_REPORT_KINDS,
                      *, max_chars: int | None = None) -> list:
    """Ask the shipped rule engine. Returns rendered lines; empty when clean.

    ``--json`` returns before the human-table branch of ``_cmd_diagnostics``, so
    a clean board prints ``[]`` rather than "No active diagnostics on this
    board." — which is why nothing here has to pattern-match prose.

    The engine is asked for every severity and the floor is applied here, so a
    kind in ``always`` (``stuck_in_blocked`` by default) is reported however low
    the engine grades it. Stuck cards are rendered from ``context`` (see
    ``blocked_context``) under their own heading; everything else keeps the
    engine's own title and detail.

    When stuck items are present, guarantees output stays <= max_chars when
    max_chars >= min_stuck_floor + 120. Below this allowance, render_stuck_cards preserves
    its documented irreducible minimum floor.
    """
    try:
        exe = str(binary) if binary else str(hermes_bin())
    except FileNotFoundError as exc:
        return [f"  diagnostics unavailable: {exc}"]
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
        return [f"  diagnostics unavailable: {type(exc).__name__}: {exc}"]
    if code != 0:
        detail = [ln for ln in ((err or out or "").strip().splitlines()) if ln.strip()]
        tail = detail[-1] if detail else f"exit {code}"
        return [f"  diagnostics unavailable: `hermes kanban diagnostics` exit {code}: {tail}"]
    try:
        entries = _parse_diagnostics_json(out)
    except json.JSONDecodeError as exc:
        return [f"  diagnostics unavailable: unparseable JSON from the CLI ({exc})"]
    # The `--json` branch of `_cmd_diagnostics` always emits a list, so this is
    # belt-and-braces — but the contract of this whole function is that it
    # returns lines and never raises, and a dict or a bare int would otherwise
    # escape as a traceback that the scheduler reports as a failed watchdog.
    if entries is not None and not isinstance(entries, list):
        return [f"  diagnostics unavailable: CLI returned "
                f"{type(entries).__name__}, expected a JSON list"]
    if max_chars is None:
        max_chars = MAX_REPORT_CHARS

    diag_lines = []
    stuck_items = []
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
                stuck_items.append((task_id, diag, entry))
                continue
            if not clears_floor(str(diag.get("severity", "")), severity) and kind not in always:
                continue
            parts = [f"  [{diag.get('severity', '?')}] {task_id} ({status}): "
                     f"{diag.get('title', diag.get('kind', 'diagnostic'))}"]
            detail = first_line((diag.get("detail") or ""), DETAIL_MAX_CHARS)
            if detail:
                parts.append(f" - {detail}")
            diag_lines.append("".join(parts))

    min_stuck_reserve = _min_stuck_floor(stuck_items, home)
    diag_allowance = max(0, max_chars - min_stuck_reserve)
    diag_budget = max(0, min(MAX_DIAGNOSTIC_CHARS, diag_allowance) - 120)
    shown_diag_lines = _take_within_budget(diag_lines, MAX_DIAGNOSTIC_ITEMS, diag_budget)
    lines = list(shown_diag_lines)
    omitted_diags = len(diag_lines) - len(shown_diag_lines)
    if omitted_diags > 0 and diag_allowance >= 120:
        lines.append(
            f"  ... and {omitted_diags} other diagnostic{'s' if omitted_diags != 1 else ''} omitted "
            f"(run hermes kanban diagnostics on the agent pod to view all)"
        )

    if stuck_items:
        current_chars = sum(len(ln) + 1 for ln in lines)
        remaining = max(0, max_chars - current_chars)
        stuck_budget = min(MAX_STUCK_CHARS, remaining)
        lines.extend(render_stuck_cards(stuck_items, context or {}, home, max_chars=stuck_budget))
    return lines


def report(home: Path, *, env=None, max_chars: int | None = None, **kwargs) -> list:
    """The whole check. Empty list means healthy, which means silent.

    Guarantees output stays <= max_chars (including 40-char main() reserve) for
    max_chars >= (MAX_DIAGNOSTIC_CHARS + min_stuck_floor + reserves). Below that threshold,
    render_stuck_cards preserves its documented minimum output floor.
    """
    if max_chars is None:
        max_chars = MAX_REPORT_CHARS
    db = board_path(home)
    if not db.exists():
        # No board on this profile: not a failure, nothing to say. Skipping the
        # shell-out here is deliberate — the CLI would create one.
        return []
    lines = []
    conn = read_only_connection(db)
    try:
        violations = invariant_violations(conn)
        context = blocked_context(conn)
    finally:
        conn.close()

    used_chars = 40  # reserve for main() header: "kanban board health: attention required\n"
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
        used_chars = 40 + sum(len(ln) + 1 for ln in lines)

    diag_header_reserve = len("Active kanban diagnostics:\n")
    remaining_chars = max(0, max_chars - used_chars - diag_header_reserve)
    diags = diagnostics_lines(
        home, severity_floor(env), env=env, context=context, always=always_report_kinds(env),
        max_chars=remaining_chars, **kwargs
    )
    if diags:
        lines.append("Active kanban diagnostics:")
        lines.extend(diags)
    return lines


def main(argv=None) -> int:
    home = agent_home()
    try:
        lines = report(home)
    except sqlite3.Error as exc:
        print(f"kanban board health: cannot read {board_path(home)}: {exc}")
        return 0
    if lines:
        print("\n".join(["kanban board health: attention required", *lines]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
