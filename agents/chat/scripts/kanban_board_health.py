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
  with a traceback, from ``gateway/kanban_watchers.py``) and survives them, so a
  log or OTel alert on that line is the right vehicle and belongs to
  observability. Do not add a query for this.
- *stale ``running`` cards.* ``detect_stale_running`` already reclaims them:
  ``kanban.dispatch_stale_timeout_seconds`` resolves to 14400 from
  ``hermes_cli/config_defaults.py`` (not from any file in this repo), and
  ``release_stale_claims`` reclaims expired claims unconditionally. Alerting on
  a state the harness is about to self-heal is how an alert gets muted.
- *cards stranded in ``ready``, stuck in ``blocked``, or failing repeatedly.*
  All three are strictly worse restatements of ``_rule_stranded_in_ready``,
  ``_rule_stuck_in_blocked`` and ``_rule_repeated_failures``, which are
  event-keyed and config-derived. They arrive through (2) or not at all.

Not scheduled, on purpose. This ships as an operator/diagnostic tool rather
than a cron job because the roster it belongs on would not carry its output to
a human. It reads the board at ``$HERMES_HOME/kanban.db``, which is the Chat
Agent's — so the Chat Agent's roster
(``agents/chat/defaults/cron/jobs.json``) is where it would go, and all four
jobs there declare ``deliver: local``, which
``cron/scheduler.py::_resolve_delivery_targets`` resolves to an empty target
list. A scheduled run would put these lines in the execution ledger and stop.
The Platform Agent's roster is the one whose jobs declare ``deliver: "all"``,
but that profile has its own cron store and no board to check. A health check
whose output goes somewhere nobody reads is worse than none, because it reads
as coverage. The script is written to the ``no_agent`` contract anyway (stdout
is the whole message, silence means healthy, exit status is always 0), so
registering it is a one-line roster addition the day there is a channel to
send it to.

Run on demand:  HERMES_HOME=/opt/data python3 kanban_board_health.py
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

# Severity floor handed to `hermes kanban diagnostics`. "error" on purpose: the
# engine's warning-tier rules are useful to pull on demand but too chatty to
# push every 15 minutes on first rollout. Override with KANBAN_HEALTH_SEVERITY.
DEFAULT_SEVERITY = "error"
SEVERITY_ENV = "KANBAN_HEALTH_SEVERITY"
SEVERITIES = ("warning", "error", "critical")

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


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


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
    return lines


def severity_floor(env=None) -> str:
    value = ((env if env is not None else os.environ).get(SEVERITY_ENV) or "").strip().lower()
    return value if value in SEVERITIES else DEFAULT_SEVERITY


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
                      binary=None, env=None) -> list:
    """Ask the shipped rule engine. Returns rendered lines; empty when clean.

    ``--json`` returns before the human-table branch of ``_cmd_diagnostics``, so
    a clean board prints ``[]`` rather than "No active diagnostics on this
    board." — which is why nothing here has to pattern-match prose.
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
    argv = [exe, "kanban", "diagnostics", "--json", "--severity", severity]
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
    lines = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        task_id = entry.get("task_id", "?")
        status = entry.get("status", "?")
        for diag in entry.get("diagnostics") or []:
            if not isinstance(diag, dict):
                continue
            parts = [f"  [{diag.get('severity', '?')}] {task_id} ({status}): "
                     f"{diag.get('title', diag.get('kind', 'diagnostic'))}"]
            detail = (diag.get("detail") or "").strip()
            if detail:
                parts.append(f" - {detail}")
            lines.append("".join(parts))
    return lines


def report(home: Path, *, env=None, **kwargs) -> list:
    """The whole check. Empty list means healthy, which means silent."""
    db = board_path(home)
    if not db.exists():
        # No board on this profile: not a failure, nothing to say. Skipping the
        # shell-out here is deliberate — the CLI would create one.
        return []
    lines = []
    conn = read_only_connection(db)
    try:
        violations = invariant_violations(conn)
    finally:
        conn.close()
    if violations:
        lines.append(
            "Board invariant violations (a direct write to kanban.db is the usual cause):"
        )
        lines.extend(violations)
    diags = diagnostics_lines(home, severity_floor(env), env=env, **kwargs)
    if diags:
        lines.append("Active kanban diagnostics:")
        lines.extend(diags)
    return lines


def main(argv=None) -> int:
    home = hermes_home()
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
