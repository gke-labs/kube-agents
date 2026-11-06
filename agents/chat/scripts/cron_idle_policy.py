#!/usr/bin/env python3
"""Hold a named profile's model-run cron jobs back while nobody talks to the install.

An install left alone after a trial keeps every prompt-driven watchdog on the
Platform Agent's roster firing at full cadence, each one a model run of up to
``max_turns`` tool calls, for as long as the pod lives. ``stall-watch`` and the
other ``no_agent`` scripts keep ``status.usage.lastActiveTime`` moving, so the
only signal that tells a person from the install's own plumbing is
``status.usage.lastHumanActiveTime`` on the ``PlatformAgent``: the operator
moves it on an inbound chat message and on nothing else. This module reads
that field and holds the prompt-driven jobs back in two stages:

* **throttle**, from :data:`DEFAULT_THROTTLE_DAYS` idle days: a due job that
  ran less than that many days ago is pushed to its first scheduled
  occurrence at or after ``last_run_at`` plus the window, so it runs at most
  once a window;
* **hibernate**, from :data:`DEFAULT_HIBERNATE_DAYS` idle days: a due job is
  pushed to its first occurrence at or after :data:`HIBERNATE_HOLD` from now,
  and again each time it comes due, until a person writes.

Jobs with ``no_agent`` prompt no model and are never touched; neither is a job
whose ``next_run_at`` Hermes has yet to compute. A missing field falls back to
the CR's creation time while ``chatMessagesInboundTotal`` counts nothing, so
an install nobody has ever messaged idles from the day it was created; with
messages counted and the field absent (an operator upgraded, or rolled back
and returned, since the last message) the signal is unknown and nothing is
held until the next one. The field counts every chat message the gateway accepted,
the agent API's included; turns through the A2A inject door are the one door
it does not count, so an install driven only through that door opts out with
``CRON_IDLE_THROTTLE_DAYS=0`` in ``spec.deployment.env`` (the operator copies
both names from there onto the agent container).

**Why ``next_run_at`` and not a skip list.** ``profile_cron_tick.py`` decides
only whether to spawn ``hermes cron tick`` against a profile, and the platform
store's ``no_agent`` entries make it spawn every few minutes; Hermes then fires
every job in the store whose ``next_run_at`` has passed. The one lever that
holds a job is the timestamp Hermes itself reads, so this module rewrites it,
under the same ``.jobs.lock`` Hermes takes, before the ticker looks for due
work. Held timestamps are computed with ``croniter`` on the expression's own
lattice, in the zone Hermes evaluates the expression in (``HERMES_TIMEZONE``,
else the profile's ``config.yaml`` ``timezone``, else UTC), so a held audit
still fires at its usual hour; an ``interval`` schedule is held by its period
instead. Hermes leaves a stored ``next_run_at`` alone until the job is claimed
or finishes, with one exception that matters here: its due scan re-arms a job
whose last run ended in ``error`` to the next legal slot when that is earlier
than the stored one, which undoes a throttle hold on such a job every day. No
extra run comes of it (the hold is written again before Hermes scans), but the
hold line repeats daily and the release on wake finds nothing to rewind.

**A run asked for by hand is not held.** ``hermes cron run <job-id>`` marks a
job due by moving ``next_run_at`` into the past, off the schedule's own grid;
a due timestamp that is not one of the expression's slots (or, for an
``interval`` job, not one period after ``last_run_at``) is such a request and
is left for the next tick to run.

**Wake.** The state file beside the store records each held job with the
timestamp written. When the stage drops (a person wrote, or the thresholds
moved) every held job whose ``next_run_at`` still equals its record is set to
its next natural occurrence after now, never to now itself, so a returning
operator gets the roster back on its schedule rather than eleven audits at
once. A job Hermes advanced meanwhile is left as Hermes left it.

**Failure posture.** The signal is read through the shell sandbox at most once
per :data:`SIGNAL_CACHE_TTL`, and only when a prompt-driven job is due or a
hold is on record, so a quiet minute costs no ``kubectl``. An unreadable
signal keeps the last reading for :data:`SIGNAL_STALE_AFTER`, then the stage
is unknown: nothing new is held and nothing is rewound, so the holds already
written expire on their own schedule. A store lock that cannot be taken skips
the write for that tick rather than writing outside it, and a missing
``croniter`` holds nothing and says so once. Nothing here raises into the
ticker: every fault is a line in its output.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

# The two knobs, read from the ticker's environment, which `spec.deployment.env`
# reaches. Days, whole numbers; `0` on the throttle disables both stages, `0`
# on hibernate disables that stage alone, and anything unparsable or negative
# means the default.
THROTTLE_DAYS_ENV = "CRON_IDLE_THROTTLE_DAYS"
HIBERNATE_DAYS_ENV = "CRON_IDLE_HIBERNATE_DAYS"
DEFAULT_THROTTLE_DAYS = 7
DEFAULT_HIBERNATE_DAYS = 14
SECONDS_PER_DAY = 86400

# How far a hibernating job is pushed each time it comes due. A day keeps the
# hold short enough that a wake the state file has lost (a replaced volume)
# still costs at most one missed day.
HIBERNATE_HOLD = timedelta(days=1)

# The signal's cache: one `kubectl` through the sandbox per TTL at most, and a
# reading older than SIGNAL_STALE_AFTER is no longer trusted to hold anything.
SIGNAL_CACHE_TTL = timedelta(minutes=5)
SIGNAL_STALE_AFTER = timedelta(days=1)

# Per profile, beside its `cron/jobs.json`. Not a template file, so the
# start-up overlay leaves it alone; a fresh volume starts with none.
STATE_FILE_NAME = "cron_idle_policy.json"
STATE_TMP_SUFFIX = ".tmp"
STATE_SCHEMA_VERSION = 1
CRON_DIR = "cron"
JOBS_FILE_NAME = "jobs.json"
PROFILES_DIR = "profiles"

# The namespace the PlatformAgent lives in, as the operator sets it on the
# agent container; the same pair `sa_role_watch.py` reads.
NAMESPACE_ENV = "KUBE_DEFAULT_NAMESPACE"
DEFAULT_NAMESPACE = "kubeagents-system"
PLATFORM_AGENT_RESOURCE = "platformagents.kubeagents.x-k8s.io"
KUBECTL_TIMEOUT_SECONDS = 30
SIGNAL_SOURCE_HUMAN = "lastHumanActiveTime"
SIGNAL_SOURCE_CREATED = "creationTimestamp"
# The counter the field is projected from. Positive with the field absent
# means an operator has yet to write the field since it started (an upgrade,
# or a rollback and return), not that nobody has ever written.
INBOUND_COUNTER = "chatMessagesInboundTotal"

# The siblings this module needs from the Platform Agent's script directory.
# In the pod every script shares one directory; a checkout keeps them under
# agents/platform/scripts, two levels up from here.
PLATFORM_SCRIPTS_DIR = Path("platform") / "scripts"
LOCK_MODULE = "profile_scaffold"
SANDBOX_MODULE = "sandbox_exec"

STAGE_ACTIVE = "active"
STAGE_THROTTLE = "throttle"
STAGE_HIBERNATE = "hibernate"
STAGE_RANK = {STAGE_ACTIVE: 0, STAGE_THROTTLE: 1, STAGE_HIBERNATE: 2}
SCHEDULE_KIND_CRON = "cron"
SCHEDULE_KIND_INTERVAL = "interval"
SECONDS_PER_MINUTE = 60
# How far an interval job's natural `next_run_at` may sit from `last_run_at`
# plus its period and still count as Hermes' own (it writes both from one
# clock reading); anything further off is a hand-marked run.
INTERVAL_SLACK = timedelta(seconds=1)

# The zone Hermes evaluates a cron expression in, read the way Hermes does:
# the variable first, then the profile's own config, else UTC.
TIMEZONE_ENV = "HERMES_TIMEZONE"
CONFIG_FILE_NAME = "config.yaml"
CONFIG_TIMEZONE_KEY = "timezone"
CRONITER_MODULE = "croniter"

# How many characters of an error reach the ticker's output line.
ERROR_EXCERPT_CHARS = 160


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def _days(environ: dict, key: str, default: int) -> int:
    raw = environ.get(key, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def settings(environ: dict | None = None) -> tuple[int, int]:
    """``(throttle_days, hibernate_days)`` as the environment sets them."""
    env = os.environ if environ is None else environ
    throttle = _days(env, THROTTLE_DAYS_ENV, DEFAULT_THROTTLE_DAYS)
    hibernate = _days(env, HIBERNATE_DAYS_ENV, DEFAULT_HIBERNATE_DAYS)
    if throttle == 0:
        hibernate = 0
    return throttle, hibernate


def stage_for(idle: timedelta, throttle_days: int, hibernate_days: int) -> str:
    """The stage an install this long idle is in under these thresholds."""
    days = idle.total_seconds() / SECONDS_PER_DAY
    if hibernate_days > 0 and days >= hibernate_days:
        return STAGE_HIBERNATE
    if throttle_days > 0 and days >= throttle_days:
        return STAGE_THROTTLE
    return STAGE_ACTIVE


# --------------------------------------------------------------------------
# the signal
# --------------------------------------------------------------------------


def _platform_module(name: str):
    here = Path(__file__).resolve().parent
    for directory in (here, here.parent.parent / PLATFORM_SCRIPTS_DIR):
        if (directory / f"{name}.py").is_file() and str(directory) not in sys.path:
            sys.path.append(str(directory))
    return importlib.import_module(name)


def parse_timestamp(value: object) -> datetime | None:
    """An offset-aware UTC datetime, or None for anything that is not one.

    Kubernetes writes RFC 3339 with a trailing ``Z``; Hermes writes
    ``isoformat()`` with an offset. A naive timestamp meant local time when it
    was written, as the ticker already reads it.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.astimezone()
    return when.astimezone(timezone.utc)


def _excerpt(text: str) -> str:
    return " ".join(text.split())[:ERROR_EXCERPT_CHARS]


def read_signal(environ: dict | None = None) -> tuple[datetime, str]:
    """When a person last wrote to the install, and which field said so.

    ``status.usage.lastHumanActiveTime`` when the CR carries it, else the CR's
    creation time while the install counts no chat message. Raises when the CR
    cannot be read, carries neither, or counts messages the field has yet to
    reflect: the signal is then unknown rather than "idle since creation".
    The namespace is ``environ``'s (the process environment by default), the
    same name the operator sets on every agent container.
    """
    env = os.environ if environ is None else environ
    sandbox_exec = _platform_module(SANDBOX_MODULE)
    namespace = str(env.get(NAMESPACE_ENV, "") or "").strip() or DEFAULT_NAMESPACE
    argv = ["kubectl", "get", PLATFORM_AGENT_RESOURCE, "-n", namespace, "-o", "json"]
    completed = sandbox_exec.run(argv, timeout=KUBECTL_TIMEOUT_SECONDS, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"kubectl get {PLATFORM_AGENT_RESOURCE} exited {completed.returncode}: "
            f"{_excerpt(completed.stderr or '')}"
        )
    listed = json.loads(completed.stdout or "null")
    items = (listed or {}).get("items") or []
    if not items:
        raise RuntimeError(f"no PlatformAgent in namespace {namespace}")
    # A webhook admits one PlatformAgent per cluster; the first is the install.
    agent = items[0]
    usage = ((agent.get("status") or {}).get("usage")) or {}
    human = parse_timestamp(usage.get(SIGNAL_SOURCE_HUMAN))
    if human is not None:
        return human, SIGNAL_SOURCE_HUMAN
    try:
        inbound = int(usage.get(INBOUND_COUNTER) or 0)
    except (TypeError, ValueError):
        inbound = 0
    if inbound > 0:
        raise RuntimeError(
            f"{SIGNAL_SOURCE_HUMAN} is absent while {INBOUND_COUNTER} counts {inbound}; "
            "the operator writes it on the next message"
        )
    created = parse_timestamp((agent.get("metadata") or {}).get(SIGNAL_SOURCE_CREATED))
    if created is not None:
        return created, SIGNAL_SOURCE_CREATED
    raise RuntimeError(
        f"the PlatformAgent in namespace {namespace} carries neither "
        f"{SIGNAL_SOURCE_HUMAN} nor {SIGNAL_SOURCE_CREATED}"
    )


# --------------------------------------------------------------------------
# state and store
# --------------------------------------------------------------------------


def empty_state() -> dict:
    return {"version": STATE_SCHEMA_VERSION, "stage": None, "signal": {}, "held": {}}


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(data, dict):
        return empty_state()
    state = empty_state()
    state["stage"] = data.get("stage") if data.get("stage") in STAGE_RANK else None
    if data.get("croniter_missing"):
        state["croniter_missing"] = True
    state["signal"] = data.get("signal") if isinstance(data.get("signal"), dict) else {}
    held = data.get("held")
    state["held"] = {
        str(k): v for k, v in held.items() if isinstance(v, dict)
    } if isinstance(held, dict) else {}
    return state


def _write_json(path: Path, payload: object) -> None:
    scratch = path.with_name(path.name + STATE_TMP_SUFFIX)
    scratch.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(scratch, path)


def _jobs_of(store: object) -> list[dict] | None:
    jobs = store.get("jobs") if isinstance(store, dict) else store
    if not isinstance(jobs, list):
        return None
    return [j for j in jobs if isinstance(j, dict)]


def _job_id(job: dict) -> str:
    return str(job.get("id") or job.get("name") or "")


def _prompt_driven(job: dict) -> bool:
    return bool(job.get("enabled", True)) and not job.get("no_agent")


def _cron_expr(job: dict) -> str | None:
    schedule = job.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("kind") != SCHEDULE_KIND_CRON:
        return None
    expr = schedule.get("expr")
    return expr if isinstance(expr, str) and expr.strip() else None


def _interval(job: dict) -> timedelta | None:
    schedule = job.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("kind") != SCHEDULE_KIND_INTERVAL:
        return None
    # A `minutes` the JSON grammar admits but a timedelta cannot hold (`1e400`,
    # an integer wider than a float) is no period, not a fault for the store.
    try:
        minutes = float(schedule.get("minutes") or 0)
        return timedelta(seconds=minutes * SECONDS_PER_MINUTE) if minutes > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _due(job: dict, now: datetime) -> bool:
    when = parse_timestamp(job.get("next_run_at"))
    return when is not None and when <= now


def croniter_available() -> bool:
    return importlib.util.find_spec(CRONITER_MODULE) is not None


def schedule_zone(profile: Path, environ: dict | None = None) -> tzinfo:
    """The zone Hermes evaluates this profile's cron expressions in."""
    env = os.environ if environ is None else environ
    name = str(env.get(TIMEZONE_ENV, "") or "").strip()
    if not name:
        try:
            import yaml

            config = yaml.safe_load((profile / CONFIG_FILE_NAME).read_text(encoding="utf-8"))
            name = str(config.get(CONFIG_TIMEZONE_KEY) or "").strip() if isinstance(config, dict) else ""
        except Exception:
            name = ""
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return timezone.utc


def next_occurrence(
    expr: str, horizon: datetime, *, inclusive: bool, zone: tzinfo = timezone.utc
) -> datetime | None:
    """The schedule's first occurrence after ``horizon``, or at it too when
    ``inclusive``, as a UTC instant. None when the expression is bad.

    The inclusive form reads ``horizon`` to the minute, the lattice a
    five-field expression lives on: a hold computed thirty seconds into the
    due minute still lands on tomorrow's slot rather than the day after's.
    The expression is walked in ``zone``, the clock Hermes reads it by.
    """
    from croniter import croniter

    base = horizon.astimezone(zone)
    if inclusive:
        base = base.replace(second=0, microsecond=0) - timedelta(seconds=1)
    try:
        return croniter(expr, base).get_next(datetime).astimezone(timezone.utc)
    except Exception:
        return None


def marked_by_hand(job: dict, due_at: datetime, zone: tzinfo) -> bool:
    """Whether a due ``next_run_at`` is a request rather than a scheduled slot.

    Every timestamp Hermes writes for a cron expression is one of its slots,
    and for an interval job one period after ``last_run_at``; ``hermes cron
    run`` marks a job due by moving the timestamp into the past, off that
    grid. A job the policy cannot classify is treated as scheduled.
    """
    expr = _cron_expr(job)
    if expr is not None:
        from croniter import croniter

        try:
            return not croniter.match(expr, due_at.astimezone(zone))
        except Exception:
            return False
    period = _interval(job)
    last_run = parse_timestamp(job.get("last_run_at"))
    if period is None or last_run is None:
        return False
    return abs((due_at - last_run) - period) > INTERVAL_SLACK


# --------------------------------------------------------------------------
# the policy
# --------------------------------------------------------------------------


def _signal(
    state: dict, now: datetime, reader: Callable[[], tuple[datetime, str]]
) -> tuple[datetime | None, str | None]:
    """The cached reading when it is fresh enough, else a new one.

    Returns ``(when, source)``, or ``(None, None)`` when nothing trustworthy is
    known. The attempt time is recorded whether or not it succeeded, so a
    failing ``kubectl`` costs one call per TTL rather than one per minute; the
    error text is kept so the ticker says it once rather than every tick.
    """
    cache = state["signal"]
    checked = parse_timestamp(cache.get("checked_at"))
    if checked is None or now - checked >= SIGNAL_CACHE_TTL or now < checked:
        cache["checked_at"] = now.isoformat()
        try:
            when, source = reader()
        except Exception as exc:  # the read is the fault; the policy carries on
            cache["error"] = _excerpt(str(exc) or type(exc).__name__)
        else:
            cache.update({"read_at": now.isoformat(), "at": when.isoformat(), "source": source})
            cache.pop("error", None)
    read_at = parse_timestamp(cache.get("read_at"))
    when = parse_timestamp(cache.get("at"))
    if read_at is None or when is None or now - read_at >= SIGNAL_STALE_AFTER:
        return None, None
    return when, str(cache.get("source") or "")


class _LockWatch:
    """Notices the lock helper falling open, which it only reports by warning."""

    def __init__(self) -> None:
        self.fell_open = False
        self.message = ""

    def __call__(self, message: str) -> None:
        self.fell_open = True
        self.message = message


def _at_or_after(job: dict, horizon: datetime, zone: tzinfo) -> datetime | None:
    """The job's first slot at or after ``horizon``; an interval job has no
    grid, so the horizon itself."""
    expr = _cron_expr(job)
    if expr is not None:
        return next_occurrence(expr, horizon, inclusive=True, zone=zone)
    return horizon if _interval(job) is not None else None


def _hold_target(
    job: dict, stage: str, now: datetime, throttle_days: int, zone: tzinfo
) -> datetime | None:
    if stage == STAGE_HIBERNATE:
        return _at_or_after(job, now + HIBERNATE_HOLD, zone)
    last_run = parse_timestamp(job.get("last_run_at"))
    if last_run is None:
        return None  # never ran: nothing to space it from, so it fires
    window = timedelta(days=throttle_days)
    if now - last_run >= window:
        return None  # a window has passed since it last ran: it fires
    return _at_or_after(job, last_run + window, zone)


def _release_target(job: dict, now: datetime, zone: tzinfo) -> datetime | None:
    """The job's next natural occurrence after ``now``: the expression's next
    slot, or for an interval job the first multiple of its period past now
    counted from its last run."""
    expr = _cron_expr(job)
    if expr is not None:
        return next_occurrence(expr, now, inclusive=False, zone=zone)
    period = _interval(job)
    if period is None:
        return None
    base = parse_timestamp(job.get("last_run_at")) or now
    elapsed = max(now - base, timedelta(0))
    try:
        return base + period * (elapsed // period + 1)
    except OverflowError:  # a period past the calendar's end has no next slot
        return None


def _record_rank(record: dict) -> int:
    # A record whose stage this version does not know is released rather than
    # kept forever.
    return STAGE_RANK.get(record.get("stage"), STAGE_RANK[STAGE_HIBERNATE])


def _apply_locked(
    store_path: Path,
    state_path: Path,
    state: dict,
    stage: str,
    now: datetime,
    throttle_days: int,
    label: str,
    zone: tzinfo,
) -> tuple[str | None, list[str]]:
    """Rewrite the store for ``stage``; returns the state as saved and the lines.

    The state is saved before the store is replaced, so a crash between the
    two leaves at worst a record the store never took, which the release
    check skips, and never a hold in the store that no wake can rewind. A
    released job's record is dropped only after the store write for the same
    reason: until then the record is what lets the next tick release it.
    """
    try:
        store = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, []
    jobs = _jobs_of(store)
    if jobs is None:
        return None, []

    held: dict[str, dict] = state["held"]
    lines: list[str] = []
    changed = False
    seen: set[str] = set()
    released: list[str] = []
    for job in jobs:
        job_id = _job_id(job)
        if not job_id or not _prompt_driven(job):
            continue
        seen.add(job_id)
        record = held.get(job_id)
        if record is not None and STAGE_RANK[stage] < _record_rank(record):
            released.append(job_id)
            if job.get("next_run_at") == record.get("next_run_at"):
                target = _release_target(job, now, zone)
                if target is not None:
                    job["next_run_at"] = target.isoformat()
                    changed = True
                    lines.append(f"{label}: released {job_id}; next run {target.isoformat()}")
            continue
        if stage == STAGE_ACTIVE or not _due(job, now):
            continue
        # A due timestamp the policy wrote itself is never a hand mark: an
        # interval job's hibernate hold sits a day on, off its period.
        own_hold = record is not None and job.get("next_run_at") == record.get("next_run_at")
        if not own_hold and marked_by_hand(job, parse_timestamp(job.get("next_run_at")), zone):
            continue  # `hermes cron run`: somebody asked for this one
        target = _hold_target(job, stage, now, throttle_days, zone)
        if target is None or target <= now:
            continue
        job["next_run_at"] = target.isoformat()
        held[job_id] = {"next_run_at": target.isoformat(), "stage": stage}
        changed = True
        lines.append(f"{label}: held {job_id} until {target.isoformat()} ({stage})")

    # A record for a job that left the roster, or was disabled, is history.
    for job_id in list(held):
        if job_id not in seen:
            held.pop(job_id)

    saved = None
    if changed:
        try:
            _write_json(state_path, state)
        except OSError as exc:
            # Nothing reaches the store without its record on disk first.
            return json.dumps(state, sort_keys=True), [
                f"{label}: cron idle policy could not save its state ({exc}); nothing held"
            ]
        saved = json.dumps(state, sort_keys=True)
        _write_json(store_path, store)
    for job_id in released:
        held.pop(job_id, None)
    return saved, lines


def apply_profile(
    profile: Path,
    now: datetime,
    environ: dict | None = None,
    reader: Callable[[], tuple[datetime, str]] | None = None,
) -> list[str]:
    """Run the policy over one profile's store; returns the lines to report."""
    store_path = profile / CRON_DIR / JOBS_FILE_NAME
    if not store_path.is_file():
        return []
    label = profile.name
    throttle_days, hibernate_days = settings(environ)
    state_path = profile / CRON_DIR / STATE_FILE_NAME
    state = load_state(state_path)
    before = json.dumps(state, sort_keys=True)

    try:
        jobs = _jobs_of(json.loads(store_path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        jobs = None
    if jobs is None:
        return []
    due_prompt = any(_prompt_driven(j) and _due(j, now) for j in jobs)
    if not due_prompt and not state["held"]:
        return []  # a quiet minute costs no signal read and no write

    lines: list[str] = []
    if throttle_days == 0:
        stage: str | None = STAGE_ACTIVE
        summary = "disabled"
    else:
        when, source = _signal(state, now, reader or read_signal)
        error = state["signal"].get("error")
        if error and error != state["signal"].get("reported_error"):
            state["signal"]["reported_error"] = error
            lines.append(f"{label}: cron idle policy cannot read the PlatformAgent ({error}); nothing held")
        elif not error:
            state["signal"].pop("reported_error", None)
        if when is None:
            stage = None
            summary = "unknown"
        else:
            idle = now - when
            stage = stage_for(idle, throttle_days, hibernate_days)
            summary = f"idle {int(idle.total_seconds() // SECONDS_PER_DAY)}d by {source} {when.isoformat()}"

    if stage is not None and stage != state["stage"]:
        # The first reading of an active install is the expected state, not news.
        if state["stage"] is not None or stage != STAGE_ACTIVE:
            lines.append(f"{label}: cron idle policy stage {state['stage'] or 'unset'} -> {stage} ({summary})")
        state["stage"] = stage
    rewind = stage is not None and any(
        STAGE_RANK[stage] < _record_rank(record) for record in state["held"].values()
    )
    if stage is not None and (rewind or (due_prompt and stage != STAGE_ACTIVE)):
        if not croniter_available():
            if not state.get("croniter_missing"):
                lines.append(f"{label}: cron idle policy cannot import {CRONITER_MODULE}; nothing held")
            state["croniter_missing"] = True
            stage = None  # fall through to the state save with nothing written
        else:
            state.pop("croniter_missing", None)
    if stage is not None and (rewind or (due_prompt and stage != STAGE_ACTIVE)):
        zone = schedule_zone(profile, environ)
        watch = _LockWatch()
        try:
            lock = _platform_module(LOCK_MODULE).cron_jobs_lock
        except Exception as exc:
            lines.append(f"{label}: cron idle policy cannot import {LOCK_MODULE} ({_excerpt(str(exc))}); nothing held")
        else:
            with lock(store_path.parent, warn=watch):
                if watch.fell_open:
                    lines.append(f"{label}: cron idle policy skipped this tick, the store lock is held ({watch.message})")
                else:
                    saved, held_lines = _apply_locked(
                        store_path, state_path, state, stage, now, throttle_days, label, zone
                    )
                    lines.extend(held_lines)
                    if saved is not None:
                        before = saved  # already on disk; save again only if it moves

    if json.dumps(state, sort_keys=True) != before:
        try:
            _write_json(state_path, state)
        except OSError as exc:
            lines.append(f"{label}: cron idle policy could not save its state ({exc})")
    return lines


def _shared_reader(reader: Callable[[], tuple[datetime, str]]) -> Callable[[], tuple[datetime, str]]:
    """One reading, or one failure, per call of :func:`apply`.

    The signal is the install's, not a profile's, so the profiles visited in
    one tick share a single ``kubectl`` and a single timeout between them.
    """
    readings: list[tuple[datetime, str]] = []
    failures: list[Exception] = []

    def read() -> tuple[datetime, str]:
        if not readings and not failures:
            try:
                readings.append(reader())
            except Exception as exc:
                failures.append(exc)
        if failures:
            raise failures[0]
        return readings[0]

    return read


def apply(home: Path, now: datetime, environ: dict | None = None) -> list[str]:
    """Run the policy over every named profile that has a store."""
    base = home / PROFILES_DIR
    if not base.is_dir():
        return []
    lines: list[str] = []
    reader = _shared_reader(lambda: read_signal(environ))
    for profile in sorted(p for p in base.iterdir() if p.is_dir()):
        try:
            lines.extend(apply_profile(profile, now, environ, reader))
        except Exception as exc:  # never the reason a tick does not happen
            lines.append(f"{profile.name}: cron idle policy failed ({_excerpt(f'{type(exc).__name__}: {exc}')})")
    return lines
