#!/usr/bin/env python3
"""Thin a named profile's catch-up fires after a pause: skip the stale, space the rest.

Hermes collapses a backlog. A recurring job whose ``next_run_at`` is further
in the past than its grace window (half the period, clamped to two minutes
and two hours: ``cron/jobs.py``, ``_compute_grace_seconds``) fires once, now,
and the occurrences in between are dropped with one ``missed_window`` row in
the ledger (``deploy/docker/patches/cron_skip_ledger.py``). That is right for
one job. It is wrong for a roster: an install paused for two days with
``spec.deployment.scaleToZero`` comes back with every model-run job past its
grace at the same instant, and every whole-estate audit starts in the first
tick, each a model run of up to ``max_turns`` tool calls, all contending for
the same slots and budget. On the install this was measured on, not one
scheduled audit finished that day and the day cost three days of steady-state
spend. This module runs inside ``profile_cron_tick.py``, after the idle policy
and before the spawn filter looks, and decides each catch-up before Hermes
scans the store:

* **skip** a catch-up that is a whole period late or more. A daily audit two
  days late has nothing to catch up on: its next slot is tomorrow's, and the
  run it would do now is the run tomorrow's slot does anyway. ``next_run_at``
  moves to the job's next slot after now and a ``skipped`` row with reason
  :data:`SKIP_REASON_STALE` goes into the job's history, since Hermes never
  sees the occurrence and so never writes its own. One slot is not skipped:
  the newest, when it came inside Hermes' grace. That run is on time, not a
  catch-up (the same job with only that slot missed would fire), so the slot
  stays and Hermes fires it on this tick; the older ones are the skip.
* **space** the catch-ups that remain, the ones inside their period. The
  oldest miss fires on this tick as Hermes has it; each later one is held
  back a further :data:`DEFAULT_STAGGER_MINUTES`, about the spacing the
  shipped roster keeps between its audit slots, and released in turn. A catch-up
  whose turn would come after the job's own next slot is skipped instead:
  that slot is its catch-up.

**Why a deferral is a hold with a record, not a timestamp a step ahead.**
Hermes checks a due cron instant against the expression before it fires it,
and re-anchors one that is not on the expression's lattice without firing
(``_reanchor_stale_cron``, a hand edit of ``jobs.json`` is what it guards
against). A daily job has no slot thirty minutes from now to be written to.
So a deferred job is held at its next natural slot, which is on the lattice
and is where it would go if the record were lost, and the state file beside
the store remembers the instant it was due and when to give it back. At that
time, if ``next_run_at`` still reads the hold, the stored string is written
back exactly as it was and the job is decided like any other due job on that
tick: as the oldest catch-up it goes to Hermes, which finds its own slot, past
its grace, and takes its own catch-up path, missed-window row included; behind
an older one it waits a further step; a whole period late, because the ticker
stopped again while it waited, it is skipped. A job Hermes or a person moved in
the meantime is left as they left it and the record dropped.

Only prompt-driven jobs are touched: a ``no_agent`` script costs no model
run and Hermes' collapse is the right answer for it. A job marked due by hand
(``hermes cron run``; the same mark the idle policy reads) is somebody's
request and runs. ``CRON_CATCH_UP_STAGGER_MINUTES`` on the agent container
sets the step; ``0`` turns the policy off and releases whatever it holds.

**Failure posture.** Nothing here raises into the ticker. A store lock that
cannot be taken skips the tick; a ledger that is missing, not yet migrated or
unreadable costs the row and a line, never the skip; a missing ``croniter``
changes nothing and says so once.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from cron_idle_policy import (
    CRON_DIR,
    JOBS_FILE_NAME,
    LOCK_MODULE,
    PROFILES_DIR,
    _cron_expr,
    _excerpt,
    _interval,
    _job_id,
    _jobs_of,
    _LockWatch,
    _platform_module,
    _prompt_driven,
    _release_target,
    _write_json,
    croniter_available,
    marked_by_hand,
    next_occurrence,
    parse_timestamp,
    schedule_zone,
    stamp,
)

# The step between released catch-ups, read from the ticker's environment,
# which `spec.deployment.env` reaches. Whole minutes; `0` turns the policy
# off, and anything unparsable or negative means the default. Thirty minutes
# is about the spacing agents/platform/cron/jobs.json keeps between its audit
# slots (most gaps are 30 minutes; the narrowest is 15).
STAGGER_MINUTES_ENV = "CRON_CATCH_UP_STAGGER_MINUTES"
DEFAULT_STAGGER_MINUTES = 30

# Hermes' grace window, mirrored so "catch-up" means the same thing on both
# sides of the store: half the period, clamped to [2 min, 2 h]
# (`_MIN_GRACE_SECONDS`, `_MAX_GRACE_SECONDS` in cron/jobs.py). A job later
# than this is one Hermes would fast-forward and fire once.
GRACE_PERIOD_DIVISOR = 2
MIN_GRACE = timedelta(seconds=120)
MAX_GRACE = timedelta(seconds=7200)

# Per profile, beside its `cron/jobs.json`, like the idle policy's.
STATE_FILE_NAME = "cron_catch_up_policy.json"
STATE_SCHEMA_VERSION = 1

# The job's history, Hermes' own file. Rows are written in the shape
# `record_skipped_execution` writes them (apply_cron_skip_ledger.py), with the
# reason the vocabulary in cron_skip_ledger.py carries under the same name.
LEDGER_FILE_NAME = "executions.db"
LEDGER_TABLE = "executions"
LEDGER_BUSY_TIMEOUT_SECONDS = 5  # Hermes' own busy timeout on this file
LEDGER_SOURCE = "builtin"
LEDGER_REASON_COLUMN = "skip_reason"
LEDGER_INSTANT_COLUMN = "scheduled_instant"
SKIPPED_STATUS = "skipped"
SKIP_REASON_STALE = "stale_catch_up"
# One id per ticker process, as Hermes stamps `_PROCESS_ID` on its rows.
PROCESS_ID = uuid.uuid4().hex

# The walk over a schedule stops here; a count past it is not what anyone
# reads the line for.
MISSED_COUNT_CAP = 10000

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400


# --------------------------------------------------------------------------
# settings and arithmetic
# --------------------------------------------------------------------------


def stagger_step(environ: dict | None = None) -> timedelta | None:
    """The step between released catch-ups, or None when the policy is off."""
    env = os.environ if environ is None else environ
    try:
        minutes = int(str(env.get(STAGGER_MINUTES_ENV, "")).strip())
        step = timedelta(minutes=minutes)
    except (TypeError, ValueError, OverflowError):  # unparsable, or wider than a timedelta
        return timedelta(minutes=DEFAULT_STAGGER_MINUTES)
    if minutes < 0:
        return timedelta(minutes=DEFAULT_STAGGER_MINUTES)
    return None if minutes == 0 else step


def grace_for(period: timedelta) -> timedelta:
    """How late a job may be before Hermes calls the run a catch-up."""
    return max(MIN_GRACE, min(period / GRACE_PERIOD_DIVISOR, MAX_GRACE))


def period_after(job: dict, stale: datetime, zone: tzinfo) -> timedelta | None:
    """The gap from the missed slot to the slot after it; None when unknown."""
    expr = _cron_expr(job)
    if expr is not None:
        following = next_occurrence(expr, stale, inclusive=False, zone=zone)
        if following is None or following <= stale:
            return None
        return following - stale
    return _interval(job)


def _wall_clock_slot(expr: str, base: datetime, zone: tzinfo, *, forward: bool) -> datetime:
    """One croniter step from ``base`` on the zone's naive wall clock, with the
    zone put back on the result, as :func:`next_occurrence` walks it."""
    from croniter import croniter

    steps = croniter(expr, base.astimezone(zone).replace(tzinfo=None))
    wall = steps.get_next(datetime) if forward else steps.get_prev(datetime)
    return wall.replace(tzinfo=zone).astimezone(timezone.utc)


def missed_occurrences(job: dict, stale: datetime, now: datetime, zone: tzinfo) -> int:
    """How many of the job's slots fell in ``[stale, now]``: the stale one
    and every one after it that has also passed."""
    period = _interval(job)
    if period is not None:
        return 1 + min(MISSED_COUNT_CAP, int((now - stale) // period))
    expr = _cron_expr(job)
    if expr is None:
        return 1
    try:
        count = 1
        cursor = stale
        while count < MISSED_COUNT_CAP:
            cursor = _wall_clock_slot(expr, cursor, zone, forward=True)
            if cursor > now:
                break
            count += 1
        return count
    except Exception:
        return 1


def newest_slot(job: dict, stale: datetime, now: datetime, zone: tzinfo) -> datetime | None:
    """The job's latest slot at or before ``now``; None when it cannot be read."""
    period = _interval(job)
    if period is not None:
        try:
            return stale + period * int((now - stale) // period)
        except OverflowError:
            return None
    expr = _cron_expr(job)
    if expr is None:
        return None
    try:
        return _wall_clock_slot(expr, now + timedelta(seconds=1), zone, forward=False)
    except Exception:
        return None


def _fmt(delta: timedelta) -> str:
    """``2d 3h``, ``1d``, ``2h 5m``, ``30m``: the two largest units, to the minute."""
    seconds = int(delta.total_seconds())
    days, rest = divmod(seconds, SECONDS_PER_DAY)
    hours, rest = divmod(rest, SECONDS_PER_HOUR)
    minutes = rest // SECONDS_PER_MINUTE
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


# --------------------------------------------------------------------------
# state and ledger
# --------------------------------------------------------------------------


def empty_state() -> dict:
    return {"version": STATE_SCHEMA_VERSION, "deferred": {}}


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    state = empty_state()
    if data.get("croniter_missing"):
        state["croniter_missing"] = True
    deferred = data.get("deferred")
    state["deferred"] = (
        {str(k): v for k, v in deferred.items() if isinstance(v, dict)} if isinstance(deferred, dict) else {}
    )
    return state


def record_skip(cron_dir: Path, job_id: str, now: datetime, instant: str, detail: str) -> str | None:
    """Write the skipped occurrence into the job's history. Returns the line
    to report when it could not, else None.

    ``instant`` is the missed slot as Hermes keys ``scheduled_instant``: the
    UTC ``isoformat()`` of the instant, whatever offset the store carried.

    Writes only into a ledger Hermes has already created and the image's
    migration has already widened: a ledger this module created would not be
    Hermes' schema, and a row the ``CHECK`` refuses is no row. The file is
    WAL-journaled and busy-timed by its owner, so a short write beside a
    running tick waits its turn rather than failing.
    """
    path = cron_dir / LEDGER_FILE_NAME
    if not path.is_file():
        return f"no history to record the skip of {job_id} in ({path} does not exist yet)"
    try:
        conn = sqlite3.connect(path, timeout=LEDGER_BUSY_TIMEOUT_SECONDS)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (LEDGER_TABLE,)
            ).fetchone()
            ddl = str(row[0] or "") if row else ""
            columns = {str(c[1]) for c in conn.execute(f"PRAGMA table_info({LEDGER_TABLE})")}
            if f"'{SKIPPED_STATUS}'" not in ddl.replace('"', "'") or LEDGER_REASON_COLUMN not in columns:
                return f"the history does not record skipped occurrences yet; the skip of {job_id} is only in this output"
            names = ["id", "job_id", "source", "process_id", "pid", "status", "claimed_at", "finished_at", "error", LEDGER_REASON_COLUMN]
            stamp = now.isoformat()
            values = [uuid.uuid4().hex, job_id, LEDGER_SOURCE, PROCESS_ID, os.getpid(), SKIPPED_STATUS, stamp, stamp, detail, SKIP_REASON_STALE]
            if LEDGER_INSTANT_COLUMN in columns:
                names.append(LEDGER_INSTANT_COLUMN)
                values.append(instant)
            with conn:
                conn.execute(
                    f"INSERT INTO {LEDGER_TABLE} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                    values,
                )
        finally:
            conn.close()
    except Exception as exc:
        return f"could not record the skip of {job_id} in the history ({_excerpt(str(exc) or type(exc).__name__)})"
    return None


# --------------------------------------------------------------------------
# the policy
# --------------------------------------------------------------------------


def _due(job: dict, now: datetime) -> bool:
    when = parse_timestamp(job.get("next_run_at"))
    return when is not None and when <= now


def _release(deferred: dict, jobs_by_id: dict, now: datetime, step: timedelta | None, label: str) -> tuple[dict, bool, list[str]]:
    """Give back every deferred job whose time has come. Returns the records
    released (taken out of ``deferred``), whether the store changed, and the
    lines.

    A released job is then an ordinary due job for the scan that follows: if
    the ticker stopped again while it waited, so that its missed slot is now a
    whole period old, it is skipped like any other stale catch-up, and two
    releases that fall on one tick are spaced again rather than fired together.
    """
    released: dict[str, dict] = {}
    changed = False
    lines: list[str] = []
    for job_id, record in list(deferred.items()):
        job = jobs_by_id.get(job_id)
        if job is None or not _prompt_driven(job):
            deferred.pop(job_id)
            continue
        if job.get("next_run_at") != record.get("next_run_at"):
            deferred.pop(job_id)
            lines.append(f"{label}: dropped the deferred catch-up of {job_id}; its schedule moved to {job.get('next_run_at')}")
            continue
        release_at = parse_timestamp(record.get("release_at"))
        restore = record.get("restore")
        if step is not None and release_at is not None and now < release_at:
            continue
        if not isinstance(restore, str) or not restore:
            deferred.pop(job_id)
            continue
        # The record stays on disk until the store holds the restored slot:
        # the caller writes it (or, when the job is deferred again on this
        # tick, the new record naming the same hold) into the state it saves
        # before the store, so a crash between the two leaves a record the
        # store still matches, never a hold with no record.
        job["next_run_at"] = restore
        released[job_id] = deferred.pop(job_id)
        changed = True
        lines.append(f"{label}: released {job_id} for its catch-up of {restore}")
    return released, changed, lines


def _apply_locked(
    store_path: Path,
    state_path: Path,
    state: dict,
    now: datetime,
    step: timedelta | None,
    label: str,
    zone: tzinfo,
) -> tuple[str | None, list[str]]:
    """Rewrite the store; returns the state as saved and the lines.

    As in the idle policy, a new record reaches disk before the store takes
    its hold, and a released record leaves the state only after the store
    has its slot back (a job deferred again on the same tick keeps a record
    naming the same hold throughout). A crash between the two writes leaves
    at worst a record the store never took, which the next tick drops, or a
    turn that slips one step, and never a hold no release can give back.
    """
    try:
        store = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, []
    jobs = _jobs_of(store)
    if jobs is None:
        return None, []
    deferred: dict = state["deferred"]
    state_before = json.dumps(state, sort_keys=True)
    jobs_by_id = {_job_id(j): j for j in jobs if _job_id(j)}

    released, changed, lines = _release(deferred, jobs_by_id, now, step, label)

    candidates: list[tuple[datetime, str, dict, timedelta, timedelta]] = []
    if step is not None:
        for job_id, job in jobs_by_id.items():
            if not _prompt_driven(job) or job_id in deferred or marked_by_hand(job):
                continue
            if isinstance(job.get("pending_slot"), dict):
                # A dispatch that advanced the slot and died before claiming
                # it. Hermes restores that slot as the due instant whatever
                # next_run_at says, so a rewrite here would be undone and the
                # row it wrote would be wrong.
                continue
            stale = parse_timestamp(job.get("next_run_at"))
            if stale is None or stale > now:
                continue
            period = period_after(job, stale, zone)
            if period is None or period <= timedelta(0):
                continue
            overdue = now - stale
            if overdue <= grace_for(period):
                continue
            candidates.append((stale, job_id, job, period, overdue))
    candidates.sort(key=lambda c: (c[0], c[1]))

    skips: list[tuple[str, str, str]] = []
    fired = 0
    for stale, job_id, job, period, overdue in candidates:
        target = _release_target(job, now, zone)
        if target is None or target <= now:
            continue  # nowhere to put it; Hermes fires it as before
        stale_text = str(job["next_run_at"])
        missed = missed_occurrences(job, stale, now, zone)
        if overdue >= period:
            # The newest slot may have come inside Hermes' grace: that run is
            # on time, not a catch-up, so it stays and fires on this tick;
            # only the slots before it are skipped.
            newest = newest_slot(job, stale, now, zone)
            if newest is not None and stale < newest <= now and now - newest <= grace_for(period):
                target = newest
                missed -= 1
            how = f"{missed} occurrence(s) missed since {stale_text}"
            why = f"{how}, {_fmt(overdue)} overdue on a {_fmt(period)} period"
        else:
            how = f"{missed} occurrence(s) missed since {stale_text}"
            try:
                release_at = now + step * fired
            except OverflowError:  # a step past the calendar's end: no turn to wait for
                release_at = target
            fired += 1
            if release_at == now:
                continue  # the oldest miss fires on this tick, as Hermes has it
            if release_at < target:
                target_text = stamp(target, zone)
                job["next_run_at"] = target_text
                deferred[job_id] = {
                    "next_run_at": target_text,
                    "restore": stale_text,
                    "release_at": release_at.isoformat(),
                }
                changed = True
                lines.append(
                    f"{label}: deferred the catch-up of {job_id} to {release_at.isoformat()} ({how}); "
                    f"held at {target_text} until then"
                )
                continue
            why = f"{how}, a catch-up {_fmt(release_at - now)} later would run at or after its next slot"
        target_text = stamp(target, zone)
        job["next_run_at"] = target_text
        changed = True
        lines.append(f"{label}: skipped the catch-up of {job_id}: {why}; next run {target_text}")
        skips.append((job_id, stale.isoformat(), f"Catch-up skipped: {why}; next run {target_text}."))

    saved = None
    if changed:
        # Saved before the store, with the released records still in it
        # (a re-deferred job's new record stands in for its old one); the
        # caller's final save drops the released ones once the store has its
        # slots back.
        pending = dict(state)
        pending["deferred"] = dict(released)
        pending["deferred"].update(deferred)
        if json.dumps(pending, sort_keys=True) != state_before:
            try:
                _write_json(state_path, pending)
            except OSError as exc:
                # Nothing reaches the store, and the released records go back
                # so the caller's final save cannot drop them either.
                deferred.update(released)
                return json.dumps(state, sort_keys=True), [
                    f"{label}: cron catch-up policy could not save its state ({exc}); nothing changed"
                ]
            saved = json.dumps(pending, sort_keys=True)
        _write_json(store_path, store)
        for job_id, instant, detail in skips:
            note = record_skip(store_path.parent, job_id, now, instant, detail)
            if note is not None:
                lines.append(f"{label}: {note}")
    return saved, lines


def apply_profile(profile: Path, now: datetime, environ: dict | None = None) -> list[str]:
    """Run the policy over one profile's store; returns the lines to report."""
    store_path = profile / CRON_DIR / JOBS_FILE_NAME
    if not store_path.is_file():
        return []
    label = profile.name
    step = stagger_step(environ)
    state_path = profile / CRON_DIR / STATE_FILE_NAME
    state = load_state(state_path)
    before = json.dumps(state, sort_keys=True)

    try:
        jobs = _jobs_of(json.loads(store_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        jobs = None
    if jobs is None:
        return []
    due_prompt = step is not None and any(
        _prompt_driven(j) and _due(j, now) and not marked_by_hand(j) for j in jobs
    )
    if not due_prompt and not state["deferred"]:
        return []  # a quiet minute costs no lock and no write

    lines: list[str] = []
    if not croniter_available():
        if not state.get("croniter_missing"):
            lines.append(f"{label}: cron catch-up policy cannot import croniter; nothing changed")
            state["croniter_missing"] = True
    else:
        state.pop("croniter_missing", None)
        zone = schedule_zone(profile, environ)
        watch = _LockWatch()
        try:
            lock = _platform_module(LOCK_MODULE).cron_jobs_lock
        except Exception as exc:
            lines.append(f"{label}: cron catch-up policy cannot import {LOCK_MODULE} ({_excerpt(str(exc))}); nothing changed")
        else:
            with lock(store_path.parent, warn=watch):
                if watch.fell_open:
                    lines.append(f"{label}: cron catch-up policy skipped this tick, the store lock is held ({watch.message})")
                else:
                    saved, policy_lines = _apply_locked(store_path, state_path, state, now, step, label, zone)
                    lines.extend(policy_lines)
                    if saved is not None:
                        before = saved  # already on disk; save again only if it moves

    if json.dumps(state, sort_keys=True) != before:
        try:
            _write_json(state_path, state)
        except OSError as exc:
            lines.append(f"{label}: cron catch-up policy could not save its state ({exc})")
    return lines


def apply(home: Path, now: datetime, environ: dict | None = None) -> list[str]:
    """Run the policy over every named profile that has a store."""
    base = home / PROFILES_DIR
    if not base.is_dir():
        return []
    lines: list[str] = []
    for profile in sorted(p for p in base.iterdir() if p.is_dir()):
        try:
            lines.extend(apply_profile(profile, now, environ))
        except Exception as exc:  # never the reason a tick does not happen
            lines.append(f"{profile.name}: cron catch-up policy failed ({_excerpt(f'{type(exc).__name__}: {exc}')})")
    return lines
