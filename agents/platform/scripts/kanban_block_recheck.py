#!/usr/bin/env python3
"""Re-check a blocked kanban card's condition, and retry the card when it has cleared.

A card that blocks stays blocked until a person acts: Retry or Drop on the
board-health card, or ``hermes kanban unblock`` on the pod. Nothing re-reads
the condition the block described. In one evaluation ten cards sat blocked
for up to thirteen days on conditions that had cleared, and the daily digest
re-reported them (#5260). This job is the re-check.

A block records a **condition** and an **expiry** in the side table
:data:`TABLE`, keyed by card and tied to the block event it describes. Two
condition kinds:

* ``time``: clears when ``check_after`` passes. Derived here, with no record
  from anyone, for the blocks the harness files itself: the dispatcher's
  circuit breaker (a ``gave_up`` event; :data:`GAVE_UP_KIND`) and a
  ``transient`` block (the rate-limit exit in ``kanban_guardrail_exit.py``,
  or a worker's ``kanban_block(kind="transient")``, the kind upstream reserves
  for a failure that may clear). The wait per kind is
  :data:`RETRY_AFTER_SECONDS`. A ``needs_input``, ``capability``, or untyped
  block is a question for a person and gets no record.
* ``k8s``: a named object on a named cluster, and the predicate its kind
  implies (:data:`PREDICATE_BY_KIND`): a pod is ``Ready``, a deployment is
  ``Available``. Read with one ``kubectl get -o json`` through the shell
  sandbox, the route ``stall_watch.py`` uses, since the agent container carries
  no kubectl; the credentials are fetched by ``stall_watch``'s rule, once a day
  per cluster or when a read says the kubeconfig is the problem, with the
  stamps in :data:`STAMPS_FILE_NAME` beside the digest's record. Recorded
  through :func:`record_condition`, or ``--watch`` on this
  script's command line on the pod; letting the worker record it when it
  blocks is a tool-text change and a follow-up (#5260).

Every :data:`JOB_ID` run derives the time records, drops a record whose card
has moved on or blocked again since, and evaluates each record whose
``check_after`` has passed and whose ``expires_at`` has not. A condition that
cleared retries the card through Hermes' ``unblock_task``, the call Retry
makes, and writes a :data:`EVENT_KIND` event with the condition and the
outcome. A condition that holds leaves the card blocked and refreshes the
record's ``last_checked_at`` and ``last_outcome``, which the board-health
digest reads into its row ("Re-checked 2 hours ago, still blocked."). Nothing
here writes a comment: upstream's ``stuck_in_blocked`` rule goes quiet after
any ``commented`` event, and the daily report is deliberate.

Bounds, so a retry cannot spin: one automatic retry per card for the card's
life (:data:`MAX_AUTO_RETRIES_PER_CARD`, counted from the card's own events; a
second clear condition is recorded as :data:`OUTCOME_RETRY_LIMIT` and waits
for a person), at most :data:`MAX_RETRIES_PER_RUN` retries per run so a
board full of old breaker trips does not spawn every worker at once, and a
``k8s`` condition stops at its expiry (:data:`OUTCOME_EXPIRED`, said once).
Upstream's own unblock-loop breaker still routes a second same-kind
``kanban_block`` to ``triage``.

``deliver: "chat"`` carries the one line a retry prints; a quiet run prints
nothing and relays nothing. Design: ``docs/designs/kanban-block-recheck.md``.

Run on demand:  PLATFORM_AGENT_HOME=/opt/data python3 kanban_block_recheck.py
Record:         python3 kanban_block_recheck.py --watch t_1234 --project P --location L \\
                    --cluster C --namespace NS --resource deployment/web [--expires-in-hours 168]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import gitops_workspace

logger = logging.getLogger(__name__)

JOB_ID = "kanban-block-recheck"
BLOCKED_STATUS = "blocked"
#: The record of each blocked card's condition, beside Hermes' own tables.
TABLE = "kanban_block_rechecks"
#: The event a re-check writes on the card: a retry, an expiry, or a retry limit.
EVENT_KIND = "block_rechecked"
CONDITION_TIME = "time"
CONDITION_K8S = "k8s"
#: The pseudo-kind for a circuit-breaker trip, which has a ``gave_up`` event
#: and no ``blocked`` one.
GAVE_UP_KIND = "gave_up"
#: The block events that put a card in ``blocked``, newest first decides.
BLOCK_EVENT_KINDS = ("blocked", GAVE_UP_KIND)
#: How long after the block a time condition clears, per kind. A rate-limit
#: storm clears in minutes; thirty gives it room. A breaker trip is two failed
#: attempts, so an hour, and the retry reads the hand-off the last attempt
#: saved (#4902).
RETRY_AFTER_SECONDS = {"transient": 30 * 60, GAVE_UP_KIND: 60 * 60}
#: One automatic retry per card. A second one would be the loop the breaker
#: exists to stop; a person decides from there.
MAX_AUTO_RETRIES_PER_CARD = 1
#: Retries per run, so the first run after an upgrade does not start every
#: old breaker trip at once; the rest wait fifteen minutes.
MAX_RETRIES_PER_RUN = 3
#: A ``k8s`` condition with no expiry given stops after a week, the digest's
#: own reminder period.
DEFAULT_K8S_EXPIRY_SECONDS = 7 * 24 * 3600
SECONDS_PER_HOUR = 3600
#: The object kinds a condition may name, and what "cleared" means for each.
PREDICATE_BY_KIND = {"pod": "Ready", "deployment": "Available"}
RESOURCE_SEPARATOR = "/"
#: A kind may be spelled in the plural kubectl also accepts (``pods/web``).
RESOURCE_PLURAL_SUFFIX = "s"
K8S_FIELDS = ("project", "location", "cluster", "namespace", "resource")
CONDITION_TRUE = "True"
KUBECTL_TIMEOUT_SECONDS = 60
KUBECTL_GET_ARGV = ("kubectl", "get")
#: Credentials follow ``stall_watch``'s rule, with its helpers: the per-cluster
#: kubeconfig it keeps in the sandbox is reused while this job's own stamp of
#: the last fetch is young enough for ``stall_watch.credentials_current`` (a
#: day), or until a read says the kubeconfig itself is the problem
#: (``stall_watch.refetch_repairs``), after which one fetch and one retry. A
#: ``get-credentials`` is a real gcloud run at the credential proxy every time
#: and buys nothing after the first. The stamps live beside the digest's record.
PROFILES_DIR = "profiles"
PLATFORM_PROFILE = "platform"
CRON_DIR = "cron"
STAMPS_FILE_NAME = "kanban_block_recheck.json"
STAMPS_TMP_SUFFIX = ".tmp"
STAMPS_KEY = "credentials"
CLUSTER_KEY_SEPARATOR = "/"
#: What kubectl prints for an object that is not there; a read that says so
#: is an answer (the condition holds), not a failure.
NOT_FOUND_MARKER = "(NotFound)"
NOT_FOUND = object()
OUTCOME_RETRIED = "retried"
OUTCOME_STILL_BLOCKED = "still_blocked"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_EXPIRED = "expired"
OUTCOME_RETRY_LIMIT = "retry_limit"
#: Outcomes after which a record is not evaluated again.
FINAL_OUTCOMES = frozenset({OUTCOME_EXPIRED, OUTCOME_RETRY_LIMIT})
DETAIL_MAX_CHARS = 300
UNTITLED_TASK = "Untitled task"
#: The lines a run prints, which ``deliver: "chat"`` posts: a retry, and the two
#: ends at which the card stays blocked for a person (said once each).
RETRIED_TIME_LINE = "Retried **{title}**: it blocked {age} ago and the wait has passed."
RETRIED_K8S_LINE = "Retried **{title}**: {resource} in {namespace} on {cluster} is {predicate} again."
RETRY_LIMIT_LINE = (
    "**{title}** is still blocked: its condition cleared, but it was already retried once, "
    "so it waits for someone."
)
EXPIRED_LINE = (
    "**{title}** is still blocked: {resource} in {namespace} on {cluster} did not become "
    "{predicate} before the re-checks ended."
)
AGE_UNITS = (("day", "days"), ("hour", "hours"), ("minute", "minutes"))
HOURS_PER_DAY = 24
MINUTES_PER_HOUR = 60
#: The board connection, as the Session KV server's card stop opens it: a plain
#: connection with Hermes' PRAGMAs and a short busy timeout, not
#: ``kanban_db_connect.connect``, whose first call in a process takes a
#: cross-process init lock, probes the whole file and runs the migrations;
#: four times an hour that is a cost with no benefit on a board the dispatcher
#: initialised.
BOARD_BUSY_TIMEOUT_SECONDS = 3.0
BOARD_PRAGMAS = (
    "synchronous=FULL",
    "wal_autocheckpoint=100",
    "journal_size_limit=8388608",
    "foreign_keys=ON",
    "secure_delete=ON",
    "cell_size_check=ON",
)
MILLISECONDS_PER_SECOND = 1000
#: ``--watch`` on a home with no board is a person's mistake, said as one.
EXIT_NO_BOARD = 1
NO_BOARD_MESSAGE = "no board at {path}: is PLATFORM_AGENT_HOME the agent's data root?"

TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    " task_id         TEXT PRIMARY KEY,"
    " block_event_id  INTEGER NOT NULL,"
    " kind            TEXT,"
    " condition       TEXT NOT NULL,"
    " check_after     INTEGER NOT NULL,"
    " expires_at      INTEGER,"
    " checks          INTEGER NOT NULL DEFAULT 0,"
    " last_checked_at INTEGER,"
    " last_outcome    TEXT,"
    " last_detail     TEXT,"
    " created_at      INTEGER NOT NULL"
    ")"
)
BLOCKED_CARDS_SQL = f"SELECT id, title FROM tasks WHERE status = '{BLOCKED_STATUS}'"
LATEST_BLOCK_EVENT_SQL = (
    "SELECT id, kind, payload, created_at FROM task_events "
    f"WHERE task_id = ? AND kind IN {BLOCK_EVENT_KINDS!r} ORDER BY id DESC LIMIT 1"
)
RECORDS_SQL = f"SELECT * FROM {TABLE} ORDER BY check_after, task_id"
RECORD_SQL = f"SELECT * FROM {TABLE} WHERE task_id = ?"
DELETE_SQL = f"DELETE FROM {TABLE} WHERE task_id = ?"
INSERT_SQL = (
    f"INSERT OR REPLACE INTO {TABLE} (task_id, block_event_id, kind, condition, check_after, expires_at, created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)
CHECKED_SQL = (
    f"UPDATE {TABLE} SET checks = checks + 1, last_checked_at = ?, last_outcome = ?, last_detail = ?"
    " WHERE task_id = ?"
)
#: The card's own ``retried`` events, read from the JSON rather than its spelling.
AUTO_RETRIES_SQL = (
    "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?"
    " AND json_extract(payload, '$.outcome') = ?"
)
TASK_STATUS_SQL = "SELECT status FROM tasks WHERE id = ?"
TASK_TITLE_SQL = "SELECT title FROM tasks WHERE id = ?"


def agent_home() -> Path:
    """The volume root that holds ``kanban.db``: ``PLATFORM_AGENT_HOME``, never
    ``HERMES_HOME``, which under the platform roster names the profile home."""
    return Path(gitops_workspace.agent_home())


def board_path(home: Path) -> Path:
    return home / "kanban.db"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(TABLE_DDL)
    conn.commit()


def _payload(row) -> dict:
    try:
        value = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def latest_block_event(conn: sqlite3.Connection, task_id: str):
    """``(event id, block kind, time)`` of the newest ``blocked``/``gave_up``
    event on the card, or None for a card blocked with no event (a direct edit)."""
    row = conn.execute(LATEST_BLOCK_EVENT_SQL, (task_id,)).fetchone()
    if row is None:
        return None
    kind = GAVE_UP_KIND if row["kind"] == GAVE_UP_KIND else _payload(row).get("kind")
    return int(row["id"]), kind, int(row["created_at"] or 0)


def time_condition_for(kind) -> dict | None:
    """The time condition a block of ``kind`` gets, or None for a kind a person must answer."""
    if kind in RETRY_AFTER_SECONDS:
        return {"kind": CONDITION_TIME}
    return None


def derive_records(conn: sqlite3.Connection, *, now: int) -> list[str]:
    """Keep the records in step with the board; returns the cards given a time record.

    A record whose card is no longer blocked, or whose block event is not the
    card's newest, is dropped first: the block it described is over. A blocked
    card with no record then gets a time record when its newest block event is
    one the harness retries (:data:`RETRY_AFTER_SECONDS`); cards blocked before
    this job existed are covered the same way, on the first run.
    """
    ensure_table(conn)
    blocked = {row["id"]: row["title"] for row in conn.execute(BLOCKED_CARDS_SQL)}
    for rec in conn.execute(RECORDS_SQL).fetchall():
        latest = latest_block_event(conn, rec["task_id"])
        if rec["task_id"] not in blocked or (latest is not None and latest[0] != rec["block_event_id"]):
            conn.execute(DELETE_SQL, (rec["task_id"],))
    conn.commit()
    recorded = []
    for task_id in sorted(blocked):
        if conn.execute(RECORD_SQL, (task_id,)).fetchone() is not None:
            continue
        latest = latest_block_event(conn, task_id)
        if latest is None:
            continue
        event_id, kind, blocked_at = latest
        condition = time_condition_for(kind)
        if condition is None:
            continue
        check_after = blocked_at + RETRY_AFTER_SECONDS[kind]
        # A time condition has no expiry: it clears at check_after.
        conn.execute(INSERT_SQL, (task_id, event_id, kind, json.dumps(condition, sort_keys=True),
                                  check_after, None, now))
        recorded.append(task_id)
    conn.commit()
    return recorded


def normalise_resource(resource) -> str:
    """``<kind>/<name>`` as kubectl is given it: the kind lower-case and singular,
    the name as written. ValueError for a kind this job cannot judge, a missing
    or padded name, or a second ``/``."""
    text = str(resource or "").strip()
    kind, sep, name = text.partition(RESOURCE_SEPARATOR)
    kind = kind.lower()
    if kind not in PREDICATE_BY_KIND and kind.endswith(RESOURCE_PLURAL_SUFFIX) and kind[:-1] in PREDICATE_BY_KIND:
        kind = kind[:-1]
    if (not sep or kind not in PREDICATE_BY_KIND or not name or name != name.strip()
            or RESOURCE_SEPARATOR in name or any(ch.isspace() for ch in name)):
        raise ValueError(
            f"resource must be <kind>/<name> with kind one of {sorted(PREDICATE_BY_KIND)}, got {resource!r}"
        )
    return f"{kind}{RESOURCE_SEPARATOR}{name}"


def resource_kind(resource: str) -> str:
    """``deployment`` of ``deployment/web``."""
    return normalise_resource(resource).partition(RESOURCE_SEPARATOR)[0]


def validate_condition(condition: dict) -> dict:
    """The condition as stored, or ValueError when it names something this job cannot read."""
    if not isinstance(condition, dict):
        raise ValueError("condition must be an object")
    kind = condition.get("kind")
    if kind == CONDITION_TIME:
        return {"kind": CONDITION_TIME}
    if kind != CONDITION_K8S:
        raise ValueError(f"condition kind must be {CONDITION_TIME!r} or {CONDITION_K8S!r}, got {kind!r}")
    missing = [field for field in K8S_FIELDS if not str(condition.get(field) or "").strip()]
    if missing:
        raise ValueError(f"a {CONDITION_K8S} condition needs {', '.join(missing)}")
    stored = {"kind": CONDITION_K8S, **{field: str(condition[field]).strip() for field in K8S_FIELDS}}
    # Validated as stored: the resource kubectl is given is the one checked here.
    stored["resource"] = normalise_resource(stored["resource"])
    return stored


def record_condition(conn: sqlite3.Connection, task_id: str, condition: dict, *, now: int,
                     check_after: int | None = None, expires_at: int | None = None) -> dict:
    """Record ``condition`` for the blocked card ``task_id``; returns the record.

    Replaces any record the card had. ``check_after`` defaults to now and
    ``expires_at`` to :data:`DEFAULT_K8S_EXPIRY_SECONDS` from it.
    """
    ensure_table(conn)
    stored = validate_condition(condition)
    row = conn.execute(TASK_STATUS_SQL, (task_id,)).fetchone()
    if row is None or row["status"] != BLOCKED_STATUS:
        raise ValueError(f"{task_id} is not a blocked card")
    latest = latest_block_event(conn, task_id)
    event_id, kind = (latest[0], latest[1]) if latest else (0, None)
    check_after = now if check_after is None else int(check_after)
    if expires_at is None and stored["kind"] == CONDITION_K8S:
        expires_at = check_after + DEFAULT_K8S_EXPIRY_SECONDS
    conn.execute(INSERT_SQL, (task_id, event_id, kind, json.dumps(stored, sort_keys=True),
                              check_after, None if expires_at is None else int(expires_at), now))
    conn.commit()
    return dict(conn.execute(RECORD_SQL, (task_id,)).fetchone())


def _condition_status(obj, condition_type: str) -> bool:
    status = obj.get("status") if isinstance(obj, dict) else None
    for cond in (status or {}).get("conditions") or ():
        if isinstance(cond, dict) and cond.get("type") == condition_type:
            return cond.get("status") == CONDITION_TRUE
    return False


def predicate_holds(resource: str, obj) -> tuple[bool, str]:
    """Whether the object clears its kind's predicate, and a word for the digest/event."""
    predicate = PREDICATE_BY_KIND[resource_kind(resource)]
    if obj is NOT_FOUND:
        return False, f"{resource} not found"
    if _condition_status(obj, predicate):
        return True, predicate
    return False, f"not {predicate}"


def _stall_watch():
    import stall_watch  # a sibling: the sandbox hop and the per-cluster kubeconfig cache

    return stall_watch


def cluster_key(condition: dict) -> str:
    return CLUSTER_KEY_SEPARATOR.join((condition["project"], condition["location"], condition["cluster"]))


def stamp(now: int) -> str:
    """A fetch time as ``stall_watch.credentials_current`` reads one."""
    return datetime.fromtimestamp(int(now), timezone.utc).isoformat()


def stamps_path(home: Path) -> Path:
    return home / PROFILES_DIR / PLATFORM_PROFILE / CRON_DIR / STAMPS_FILE_NAME


def load_stamps(path: Path) -> dict:
    """The per-cluster fetch stamps; empty (so the next check fetches) when missing or unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    stamps = data.get(STAMPS_KEY) if isinstance(data, dict) else None
    return {str(k): str(v) for k, v in stamps.items()} if isinstance(stamps, dict) else {}


def save_stamps(path: Path, stamps: dict) -> None:
    """Write the stamps atomically; a write that fails costs one more fetch next run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + STAMPS_TMP_SUFFIX)
        tmp.write_text(json.dumps({STAMPS_KEY: stamps}, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("%s: could not save the credential stamps at %s: %r", JOB_ID, path, exc)


def _kubectl_get(stall, condition: dict, kubeconfig: str):
    argv = [*KUBECTL_GET_ARGV, condition["resource"], "-n", condition["namespace"], "-o", "json"]
    return stall.run_sandbox(argv, timeout=KUBECTL_TIMEOUT_SECONDS, kubeconfig=kubeconfig)


def kube_get(condition: dict, *, stall=None, stamps: dict | None = None, now: int | None = None):
    """The object a ``k8s`` condition names, as kubectl's JSON; :data:`NOT_FOUND`
    when the cluster says so; RuntimeError for any other failure.

    One ``kubectl get`` on the cluster's kubeconfig. Credentials are fetched
    first only when ``stamps`` holds no fetch within the day for the cluster,
    and once more when the read fails for a reason ``stall_watch`` knows a
    fetch repairs. As in ``stall_watch``, a fetch is stamped only once a read
    on it has worked (an answer of "not found" included), and a stamp whose
    read failed for the kubeconfig is dropped, so the next run fetches first
    rather than failing the same read again; ``stamps`` is updated in place.
    """
    stall = stall or _stall_watch()
    stamps = {} if stamps is None else stamps
    now = int(time.time()) if now is None else int(now)
    key = cluster_key(condition)
    where = (condition["project"], condition["cluster"], condition["location"])
    fetched = False
    if not stall.credentials_current(stamps.get(key), stamp(now)):
        stamps.pop(key, None)
        kubeconfig = stall.fetch_credentials(*where)
        fetched = True
    else:
        kubeconfig = stall.kubeconfig_path(*where)
    proc = _kubectl_get(stall, condition, kubeconfig)
    if proc.returncode != 0 and not fetched:
        failure = RuntimeError((proc.stderr or "").strip())
        if NOT_FOUND_MARKER not in str(failure) and stall.refetch_repairs(failure):
            stamps.pop(key, None)
            kubeconfig = stall.fetch_credentials(*where)
            fetched = True
            proc = _kubectl_get(stall, condition, kubeconfig)
    stderr = (proc.stderr or "").strip()
    if proc.returncode == 0 or NOT_FOUND_MARKER in stderr:
        if fetched:
            stamps[key] = stamp(now)
        return NOT_FOUND if proc.returncode != 0 else json.loads(proc.stdout)
    raise RuntimeError(f"kubectl get exited {proc.returncode}: {stderr[:DETAIL_MAX_CHARS]}")


def evaluate(condition: dict, *, kube) -> tuple[bool, str]:
    """``(cleared, detail)`` for a due condition. Raises for a read that failed."""
    if condition.get("kind") == CONDITION_TIME:
        return True, CONDITION_TIME
    return predicate_holds(condition["resource"], kube(condition))


def auto_retries(conn: sqlite3.Connection, task_id: str) -> int:
    """How many times this job has already retried the card."""
    return int(conn.execute(AUTO_RETRIES_SQL, (task_id, EVENT_KIND, OUTCOME_RETRIED)).fetchone()[0])


def _default_unblock(conn: sqlite3.Connection, task_id: str) -> bool:
    from hermes_cli import kanban_db

    return bool(kanban_db.unblock_task(conn, task_id))


def _default_append_event(conn: sqlite3.Connection, task_id: str, payload: dict) -> None:
    from hermes_cli import kanban_db

    with kanban_db.write_txn(conn):
        kanban_db._append_event(conn, task_id, EVENT_KIND, payload)


def plain_age(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, hours, minutes = seconds // (HOURS_PER_DAY * SECONDS_PER_HOUR), seconds // SECONDS_PER_HOUR, seconds // MINUTES_PER_HOUR
    count, units = (days, AGE_UNITS[0]) if days else (hours, AGE_UNITS[1]) if hours else (minutes, AGE_UNITS[2])
    return f"{count} {units[0] if count == 1 else units[1]}"


def retried_line(title: str, condition: dict, blocked_for: int) -> str:
    title = " ".join(str(title or "").split()) or UNTITLED_TASK
    if condition.get("kind") == CONDITION_K8S:
        return RETRIED_K8S_LINE.format(
            title=title, resource=condition["resource"], namespace=condition["namespace"],
            cluster=condition["cluster"], predicate=PREDICATE_BY_KIND[resource_kind(condition["resource"])],
        )
    return RETRIED_TIME_LINE.format(title=title, age=plain_age(blocked_for))


def _mark(conn: sqlite3.Connection, task_id: str, now: int, outcome: str, detail: str) -> None:
    conn.execute(CHECKED_SQL, (now, outcome, detail[:DETAIL_MAX_CHARS], task_id))
    conn.commit()


def _still_this_block(conn: sqlite3.Connection, rec) -> bool:
    """Whether the card is still ``blocked`` by the block the record describes.

    Re-read before every write: a slow cluster read earlier in the run is time
    enough for a person's Retry (an ``unblocked`` event, the card ``ready``), a
    Drop, or a worker's new block. A record that no longer holds is dropped.
    """
    row = conn.execute(TASK_STATUS_SQL, (rec["task_id"],)).fetchone()
    latest = latest_block_event(conn, rec["task_id"])
    if row is not None and row["status"] == BLOCKED_STATUS and (
            latest is None or latest[0] == int(rec["block_event_id"])):
        return True
    conn.execute(DELETE_SQL, (rec["task_id"],))
    conn.commit()
    return False


def recheck(conn: sqlite3.Connection, *, now: int | None = None, kube=None, unblock=None, append_event=None,
            stamps_path: Path | None = None) -> list[str]:
    """One run: derive, evaluate what is due, retry what cleared. Returns the lines to say.

    ``kube``, ``unblock`` and ``append_event`` default to the sandbox read and
    Hermes' own writers; a test stands them in. ``stamps_path`` is where the
    credential stamps persist between runs (none: this run only). One card's
    failure is logged and does not stop the rest.
    """
    now = int(time.time()) if now is None else int(now)
    stamps = load_stamps(stamps_path) if stamps_path else {}
    if kube is None:
        def kube(condition, _stamps=stamps):
            return kube_get(condition, stamps=_stamps, now=now)
    unblock = unblock or _default_unblock
    append_event = append_event or _default_append_event
    derive_records(conn, now=now)
    lines: list[str] = []
    retries = 0
    for rec in conn.execute(RECORDS_SQL).fetchall():
        task_id = rec["task_id"]
        try:
            if rec["last_outcome"] in FINAL_OUTCOMES or now < int(rec["check_after"]):
                continue
            condition = json.loads(rec["condition"])
            title_row = conn.execute(TASK_TITLE_SQL, (task_id,)).fetchone()
            title = title_row["title"] if title_row else ""
            event_payload = {"condition": condition, "checked_at": now, "block_event_id": rec["block_event_id"]}
            if rec["expires_at"] is not None and now > int(rec["expires_at"]):
                if not _still_this_block(conn, rec):
                    continue
                _mark(conn, task_id, now, OUTCOME_EXPIRED, "")
                append_event(conn, task_id, {**event_payload, "outcome": OUTCOME_EXPIRED})
                lines.append(ended_line(title, condition, EXPIRED_LINE))
                continue
            if retries >= MAX_RETRIES_PER_RUN:
                continue
            try:
                cleared, detail = evaluate(condition, kube=kube)
            except Exception as exc:  # noqa: BLE001 — a cluster that cannot be read is an answer
                _mark(conn, task_id, now, OUTCOME_UNKNOWN, f"{type(exc).__name__}: {exc}")
                continue
            if not cleared:
                _mark(conn, task_id, now, OUTCOME_STILL_BLOCKED, detail)
                continue
            if not _still_this_block(conn, rec):
                continue
            if auto_retries(conn, task_id) >= MAX_AUTO_RETRIES_PER_CARD:
                _mark(conn, task_id, now, OUTCOME_RETRY_LIMIT, detail)
                append_event(conn, task_id, {**event_payload, "outcome": OUTCOME_RETRY_LIMIT, "detail": detail})
                lines.append(ended_line(title, condition, RETRY_LIMIT_LINE))
                continue
            latest = latest_block_event(conn, task_id)
            blocked_at = latest[2] if latest else now
            # The event goes first, so the once-per-card bound holds even if the
            # unblock lands and this process dies before a second write. An
            # unblock that then finds the card gone leaves a retried event on a
            # card that did not retry; the window is the write itself.
            append_event(conn, task_id, {**event_payload, "outcome": OUTCOME_RETRIED, "detail": detail})
            unblocked = unblock(conn, task_id)
            conn.execute(DELETE_SQL, (task_id,))
            conn.commit()
            if not unblocked:
                continue
            retries += 1
            lines.append(retried_line(title, condition, now - blocked_at))
            logger.info("%s: retried %s (%s)", JOB_ID, task_id, detail)
        except Exception as exc:  # noqa: BLE001 — one card must not stop the rest
            logger.warning("%s: could not re-check %s: %r", JOB_ID, task_id, exc)
    if stamps_path:
        save_stamps(stamps_path, stamps)
    return lines


def ended_line(title: str, condition: dict, template: str) -> str:
    """``RETRY_LIMIT_LINE`` or ``EXPIRED_LINE`` for the card: the two ends at
    which it stays blocked for a person."""
    title = " ".join(str(title or "").split()) or UNTITLED_TASK
    fields = {"title": title}
    if condition.get("kind") == CONDITION_K8S:
        fields.update(resource=condition["resource"], namespace=condition["namespace"], cluster=condition["cluster"],
                      predicate=PREDICATE_BY_KIND[resource_kind(condition["resource"])])
    return template.format(**fields)


def _board_connect(db: Path) -> sqlite3.Connection:
    """The board as the card stop opens it (``BOARD_PRAGMAS``): registered with
    Hermes' ``connect_tracked`` when this Hermes has one, so its byte-level
    probes of the file refuse to close under this connection."""
    try:
        from hermes_cli.sqlite_safe_read import connect_tracked
    except ImportError:
        connect_tracked = None
    kwargs = {"timeout": BOARD_BUSY_TIMEOUT_SECONDS, "isolation_level": None}
    if connect_tracked is not None:
        conn = connect_tracked(str(db), connect_fn=sqlite3.connect, **kwargs)
    else:
        conn = sqlite3.connect(str(db), **kwargs)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(BOARD_BUSY_TIMEOUT_SECONDS * MILLISECONDS_PER_SECOND)}")
        for pragma in BOARD_PRAGMAS:
            conn.execute(f"PRAGMA {pragma}")
    except Exception:
        conn.close()
        raise
    return conn


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Re-check blocked kanban cards, or record a card's condition.")
    parser.add_argument("--watch", metavar="TASK_ID", help="record a Kubernetes condition on this blocked card")
    for field in K8S_FIELDS:
        parser.add_argument(f"--{field}", help=f"the condition's {field}" + (" (<kind>/<name>)" if field == "resource" else ""))
    parser.add_argument("--expires-in-hours", type=float, default=None,
                        help=f"stop re-checking after this long (default {DEFAULT_K8S_EXPIRY_SECONDS // SECONDS_PER_HOUR})")
    return parser.parse_args(argv)


def main(argv=None, *, conn: sqlite3.Connection | None = None, now: int | None = None,
         kube=None, unblock=None, append_event=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    now = int(time.time()) if now is None else int(now)
    own = conn is None
    if own:
        db = board_path(agent_home())
        if not db.exists():
            # No board on this home: nothing for the job to do, a mistake for a person.
            if args.watch:
                print(NO_BOARD_MESSAGE.format(path=db), file=sys.stderr)
                return EXIT_NO_BOARD
            return 0
        conn = _board_connect(db)
    try:
        if args.watch:
            condition = {"kind": CONDITION_K8S, **{field: getattr(args, field) for field in K8S_FIELDS}}
            expires_at = None if args.expires_in_hours is None else now + int(args.expires_in_hours * SECONDS_PER_HOUR)
            rec = record_condition(conn, args.watch, condition, now=now, expires_at=expires_at)
            print(f"{args.watch}: re-checking {rec['condition']} every run until {rec['expires_at']}")
            return 0
        for line in recheck(conn, now=now, kube=kube, unblock=unblock, append_event=append_event,
                            stamps_path=stamps_path(agent_home()) if own else None):
            print(line)
        return 0
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
