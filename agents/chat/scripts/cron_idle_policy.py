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
whose ``next_run_at`` Hermes has yet to compute, or whose schedule is not a
cron expression. A missing field falls back to the CR's creation time, so an
install nobody has ever messaged idles from the day it was created. Turns
through the A2A inject door and the agent API do not move the field either,
by the field's own definition; an install driven only by them opts out with
``CRON_IDLE_THROTTLE_DAYS=0`` in ``spec.deployment.env``.

**Why ``next_run_at`` and not a skip list.** ``profile_cron_tick.py`` decides
only whether to spawn ``hermes cron tick`` against a profile, and the platform
store's ``no_agent`` entries make it spawn every few minutes; Hermes then fires
every job in the store whose ``next_run_at`` has passed. The one lever that
holds a job is the timestamp Hermes itself reads, so this module rewrites it,
under the same ``.jobs.lock`` Hermes takes, before the ticker looks for due
work. Hermes rewrites ``next_run_at`` only when it claims or finishes a run, so
a held timestamp stays where it was put. Held timestamps are computed with
``croniter`` on the expression's own lattice, so a held audit still fires at
its usual hour.

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
the write for that tick rather than writing outside it. Nothing here raises
into the ticker: every fault is a line in its output.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

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


def read_signal() -> tuple[datetime, str]:
    """When a person last wrote to the install, and which field said so.

    ``status.usage.lastHumanActiveTime`` when the CR carries it, else the CR's
    creation time. Raises when the CR cannot be read or carries neither.
    """
    sandbox_exec = _platform_module(SANDBOX_MODULE)
    namespace = os.environ.get(NAMESPACE_ENV, "").strip() or DEFAULT_NAMESPACE
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


def _due(job: dict, now: datetime) -> bool:
    when = parse_timestamp(job.get("next_run_at"))
    return when is not None and when <= now


def next_occurrence(expr: str, horizon: datetime, *, inclusive: bool) -> datetime | None:
    """The schedule's first occurrence after ``horizon``, or at it too when
    ``inclusive``. None when croniter is missing or the expression is bad.

    The inclusive form reads ``horizon`` to the minute, the lattice a
    five-field expression lives on: a hold computed thirty seconds into the
    due minute still lands on tomorrow's slot rather than the day after's.
    """
    try:
        from croniter import croniter
    except ImportError:
        return None
    if inclusive:
        base = horizon.replace(second=0, microsecond=0) - timedelta(seconds=1)
    else:
        base = horizon
    try:
        return croniter(expr, base.astimezone(timezone.utc)).get_next(datetime)
    except Exception:
        return None


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


def _hold_target(job: dict, stage: str, now: datetime, throttle_days: int) -> datetime | None:
    expr = _cron_expr(job)
    if expr is None:
        return None
    if stage == STAGE_HIBERNATE:
        return next_occurrence(expr, now + HIBERNATE_HOLD, inclusive=True)
    last_run = parse_timestamp(job.get("last_run_at"))
    if last_run is None:
        return None  # never ran: nothing to space it from, so it fires
    window = timedelta(days=throttle_days)
    if now - last_run >= window:
        return None  # a window has passed since it last ran: it fires
    return next_occurrence(expr, last_run + window, inclusive=True)


def _record_rank(record: dict) -> int:
    # A record whose stage this version does not know is released rather than
    # kept forever.
    return STAGE_RANK.get(record.get("stage"), STAGE_RANK[STAGE_HIBERNATE])


def _apply_locked(
    store_path: Path, state: dict, stage: str, now: datetime, throttle_days: int, label: str
) -> tuple[bool, list[str]]:
    """Rewrite the store for ``stage``; returns whether it changed and the lines."""
    try:
        store = json.loads(store_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, []
    jobs = _jobs_of(store)
    if jobs is None:
        return False, []

    held: dict[str, dict] = state["held"]
    lines: list[str] = []
    changed = False
    seen: set[str] = set()
    for job in jobs:
        job_id = _job_id(job)
        if not job_id or not _prompt_driven(job):
            continue
        seen.add(job_id)
        record = held.get(job_id)
        if record is not None and STAGE_RANK[stage] < _record_rank(record):
            held.pop(job_id, None)
            expr = _cron_expr(job)
            if job.get("next_run_at") == record.get("next_run_at") and expr is not None:
                target = next_occurrence(expr, now, inclusive=False)
                if target is not None:
                    job["next_run_at"] = target.isoformat()
                    changed = True
                    lines.append(f"{label}: released {job_id}; next run {target.isoformat()}")
            continue
        if stage == STAGE_ACTIVE or not _due(job, now):
            continue
        target = _hold_target(job, stage, now, throttle_days)
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

    if changed:
        _write_json(store_path, store)
    return changed, lines


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
                    _, held_lines = _apply_locked(store_path, state, stage, now, throttle_days, label)
                    lines.extend(held_lines)

    if json.dumps(state, sort_keys=True) != before:
        try:
            _write_json(state_path, state)
        except OSError as exc:
            lines.append(f"{label}: cron idle policy could not save its state ({exc})")
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
            lines.append(f"{profile.name}: cron idle policy failed ({_excerpt(f'{type(exc).__name__}: {exc}')})")
    return lines
