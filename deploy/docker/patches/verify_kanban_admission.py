#!/usr/bin/env python3
"""Build gate for the kanban admission patch.

Run by ``deploy/docker/Dockerfile`` from ``/opt/hermes`` after
``apply_kanban_admission.py``. The applier proves five anchors matched once;
this proves the engine those edits produce admits and refuses the way the
module says, twice over:

1. Against a fixture: a miniature Hermes tree whose dispatcher carries the
   v2026.9.14 text of the functions the anchors sit in (``_run_reclaim_phase``,
   ``_tick_spawn_budget``, ``_dispatch_once_locked``), applied here and
   driven in a child process. This half runs anywhere, with no Hermes
   installed, so a reviewer can run it from the repository.
2. Against the real tree under ``HERMES_ROOT`` when it carries the patch:
   the same drive through the real ``dispatch_once``, ``create_task``,
   ``claim_task`` and ``block_task`` on a real board.

The drive, in both: a tick under a critical budget sample spawns nothing and
an elevated one spawns one, whatever ``/proc/meminfo`` says; unreadable
counters fail closed, or fall back to upstream when ``fail_closed`` is off;
a worker over its RSS cap (a real ``sleep`` child) is stopped and its card
blocked with kind ``capacity`` and the figures in the reason; a ready card
past the queue bound is refused only on a tick that was capacity-limited (a
full slot cap, memory, an exhausted budget), never a fresh one, never one the
tick skipped as unassigned, never in a dry run. The applier is also shown to
be idempotent, and every name its inserts read is shown to be bound.

Usage::

    cd /opt/hermes && python3 verify_kanban_admission.py
    python3 deploy/docker/patches/verify_kanban_admission.py   # fixture half only
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import apply_kanban_admission as applier  # noqa: E402
import kanban_admission as admission  # noqa: E402
import patchlib  # noqa: E402

HERMES = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))

# The child-process flag for the fixture drive: ``--drive <root>``.
DRIVE_FLAG = "--drive"

# The sibling applier whose anchor edit 2 must leave exactly once.
FANOUT_REAPER_ANCHOR = "    result.timed_out = enforce_max_runtime(conn)\n"

# Figures the drive uses, in MiB. The budget is the operator default; the
# three usages sit in upstream's ok, elevated (< 15% available) and critical
# (< 5% or < 64 MiB available) bands against it.
BUDGET_MIB = 4096  # the budget the drive classifies against; elevated keeps room for one worker at this size
USAGE_OK_MIB = 800  # 3,296 MiB free: ok, six workers of headroom
SMALL_BUDGET_MIB = 2048  # the stock request today, where elevated leaves under one reserve
SMALL_USAGE_MIB = 1900  # 148 MiB free: elevated, no room for a worker
USAGE_ELEVATED_MIB = 3500  # 596 MiB free: under 15% of the budget, one worker reserve of headroom
USAGE_CRITICAL_MIB = 3950  # 146 MiB free: under 5% of the budget
WORKER_CAP_MIB = 100
WORKER_OVER_CAP_MIB = 150
WORKER_UNDER_CAP_MIB = 10
QUEUE_BOUND_SECONDS = 60
OLD_AGE_SECONDS = 300
OLDER_AGE_SECONDS = 400
OLDEST_AGE_SECONDS = 500
YOUNG_AGE_SECONDS = 200
KIB_PER_MIB = 1024
# A sample upstream's own /proc/meminfo reader would return on an idle host.
PLENTIFUL_SAMPLE = {admission.TOTAL_KEY: 32 * 1024 * 1024, admission.AVAILABLE_KEY: 20 * 1024 * 1024}
# The sibling kanban_priority patch puts user cards at or above this.
USER_PRIORITY = 100
# How long the sleep child may live, how long to wait for its death and how
# often to look, and how long ``ps`` may take to answer about it.
SLEEP_SECONDS = "300"
WAIT_SECONDS = 10
POLL_INTERVAL_SECONDS = 0.1
PS_TIMEOUT_SECONDS = 5
PS_STATE_COMMAND = ("ps", "-o", "stat=", "-p")
ZOMBIE_STATE = "Z"
PROFILE = "platform"

FAILURES: list = []


def check(label: str, condition: object, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
        return
    FAILURES.append(f"{label}{': ' + detail if detail else ''}")
    print(f"  FAIL {label}{': ' + detail if detail else ''}")


# ---------------------------------------------------------------------------
# The fixture tree
# ---------------------------------------------------------------------------

FIXTURE_DB = '''"""Fixture: hermes_cli/kanban_db.py at v2026.9.14, cut to what the admission patch reaches."""
from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import socket
import sqlite3
import subprocess
import time
from typing import Any, Optional

_log = logging.getLogger(__name__)

# Typed block reasons (routing in ``_route_block``); ``None`` = legacy un-typed.
VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, assignee TEXT, status TEXT NOT NULL,
    priority INTEGER DEFAULT 0, created_at INTEGER NOT NULL, started_at INTEGER,
    claim_lock TEXT, claim_expires INTEGER, worker_pid INTEGER, current_run_id INTEGER,
    block_kind TEXT
);
CREATE TABLE IF NOT EXISTS task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, status TEXT NOT NULL,
    started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
    kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
);
"""


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


@contextlib.contextmanager
def write_txn(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _claimer_id() -> str:
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def _host_prefix() -> str:
    return f"{_claimer_id().split(':', 1)[0]}:"


def _pid_alive(pid) -> bool:
    """Upstream's shape: existence, then a zombie is dead (Linux /proc, else ps)."""
    if not pid or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        with open(f"/proc/{int(pid)}/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("State:"):
                    return not line.split()[1].startswith("Z")
    except OSError:
        pass
    try:
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return True
    return bool(out) and not out.startswith("Z")


def _append_event(conn, task_id, kind, payload=None, *, run_id=None) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, json.dumps(payload) if payload is not None else None, int(time.time())),
    )


def create_task(conn, *, title, assignee=None, priority=0) -> str:
    task_id = "t_" + secrets.token_hex(4)
    now = int(time.time())
    with write_txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, priority, created_at) VALUES (?, ?, ?, 'ready', ?, ?)",
            (task_id, title, assignee, priority, now),
        )
        _append_event(conn, task_id, "created", {"title": title})
    return task_id


def claim_task(conn, task_id, *, claimer=None) -> Optional[str]:
    now = int(time.time())
    lock = claimer or _claimer_id()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, started_at = ? "
            "WHERE id = ? AND status = 'ready' AND claim_lock IS NULL",
            (lock, now + 900, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        run = conn.execute(
            "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', ?)", (task_id, now),
        )
        conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run.lastrowid, task_id))
        _append_event(conn, task_id, "claimed", {"claimer": lock}, run_id=run.lastrowid)
    return task_id


def block_task(conn, task_id, *, reason=None, kind=None, expected_run_id=None) -> bool:
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None")
    with write_txn(conn):
        sql = (
            "UPDATE tasks SET status = 'blocked', claim_lock = NULL, claim_expires = NULL, "
            "worker_pid = NULL, block_kind = ? WHERE id = ? AND status IN ('running', 'ready')"
        )
        params: tuple = (kind, task_id)
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params = (*params, int(expected_run_id))
        if conn.execute(sql, params).rowcount != 1:
            return False
        conn.execute(
            "UPDATE task_runs SET status = 'blocked', outcome = 'blocked', ended_at = ? "
            "WHERE task_id = ? AND ended_at IS NULL",
            (int(time.time()), task_id),
        )
        _append_event(conn, task_id, "blocked", {"reason": reason, "kind": kind})
    return True


def recompute_ready(conn, failure_limit=None) -> int:
    return 0


def release_stale_claims(conn, *, signal_fn=None) -> int:
    return 0
'''

# The dispatcher fixture. Everything the five anchors sit in is upstream's
# v2026.9.14 text verbatim (``DispatchResult``'s fields, ``_kill_fn``,
# ``_poll_worker_exit``, ``_sigkill``, ``_memory_pressure_level``,
# ``_run_reclaim_phase``, ``_tick_spawn_budget``, ``_lane_rows``,
# ``_dispatch_once_locked``, ``_positive_int``); the callees those reach are
# stubs that do the one thing the drive needs of them.
FIXTURE_DISPATCH = '''"""Fixture: hermes_cli/kanban_db_dispatch.py at v2026.9.14, cut to the admission anchors."""
from __future__ import annotations

import os
import signal
import sqlite3
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Callable
from typing import Mapping
from typing import Optional

DEFAULT_FAILURE_LIMIT = 2


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass."""

    reclaimed: int = 0
    promoted: int = 0
    reconciled_orphans: list[str] = field(default_factory=list)
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    skipped_unassigned: list[str] = field(default_factory=list)
    auto_assigned_default: list[str] = field(default_factory=list)
    skipped_nonspawnable: list[str] = field(default_factory=list)
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    crashed: list[str] = field(default_factory=list)
    auto_blocked: list[str] = field(default_factory=list)
    timed_out: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)
    skipped_locked: bool = False
    memory_pressure: Optional[str] = None


def _kill_fn(signal_fn) -> Optional[Callable[[int, int], None]]:
    """``signal_fn`` test hook, else ``os.kill`` when the platform has one."""
    if signal_fn is not None:
        return signal_fn
    return os.kill if hasattr(os, "kill") else None


def _poll_worker_exit(pid: int) -> bool:
    """Poll ~5 s (10 x 0.5 s) for ``pid`` to die; True once it is gone."""
    for _ in range(10):
        if not _kb._pid_alive(pid):
            return True
        time.sleep(0.5)
    return False


def _sigkill(kill, pid: int) -> bool:
    """Best-effort SIGKILL; True when the signal was delivered."""
    try:
        # signal.SIGKILL doesn't exist on Windows; SIGTERM maps to TerminateProcess.
        kill(int(pid), getattr(signal, "SIGKILL", signal.SIGTERM))
        return True
    except (ProcessLookupError, OSError):
        return False


def _pid_alive(pid: Optional[int]) -> bool:
    return _kb._pid_alive(pid)


def enforce_max_runtime(conn: sqlite3.Connection, *, signal_fn=None) -> list[str]:
    return []


def reap_worker_zombies() -> "list[int]":
    return []


def reconcile_orphaned_running(conn: sqlite3.Connection) -> list[str]:
    return []


def detect_stale_running(conn: sqlite3.Connection, *, stale_timeout_seconds: int) -> list[str]:
    return []


def detect_crashed_workers(conn: sqlite3.Connection) -> list[str]:
    return []


def count_running_tasks(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'").fetchone()[0])


def count_running_tasks_other_boards(board: Optional[str] = None) -> int:
    return 0


def review_dispatch_enabled() -> bool:
    return False


def _any_spawnable_review(review_rows: list[sqlite3.Row]) -> bool:
    return False


def _resolve_default_assignee(default_assignee: Optional[str]) -> Optional[str]:
    return (default_assignee or "").strip() or None


def _profile_exists_fn() -> Optional[Callable[[str], bool]]:
    return None


def check_respawn_guard(conn: sqlite3.Connection, task_id: str, *, lane: str = "ready") -> Optional[str]:
    return None


def _apply_default_assignee(conn, task_id, assignee, *, dry_run=False) -> bool:
    conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (assignee, task_id))
    conn.commit()
    return True


def _system_memory_sample() -> dict:
    return {}


def _memory_pressure_level(sample: Optional[Mapping[str, Any]] = None) -> str:
    """Classify system memory pressure: ok/elevated/critical/unknown.

    Reuses :func:`gateway.memory_status.classify_pressure` so "critical" matches
    the dashboard banner and lifecycle-ledger OOM heuristics. ``unknown``
    (non-Linux, read failure) imposes no restriction — never brick dispatch
    where /proc is unavailable.
    """
    if sample is None:
        sample = _system_memory_sample()
    if not sample:
        return "unknown"
    try:
        from gateway.memory_status import classify_pressure
        return classify_pressure(sample.get("mem_available_kib"), sample.get("mem_total_kib"))
    except Exception:
        return "unknown"


def _dispatch_lane_task(conn, row, assignee, result, *, lane, dry_run, ttl_seconds, board,
                        failure_limit, spawn_fn, per_profile_cap, per_profile_running) -> bool:
    task_id = row["id"]
    if dry_run:
        result.spawned.append((task_id, assignee, ""))
        return True
    if _kb.claim_task(conn, task_id) is None:
        return False
    pid = spawn_fn(task_id) if spawn_fn is not None else None
    if pid:
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (int(pid), task_id))
        conn.commit()
    result.spawned.append((task_id, assignee, ""))
    return True


def _run_reclaim_phase(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    stale_timeout_seconds: int,
    failure_limit: int,
    reconcile_orphans: bool,
) -> None:
    """Reclaim stale/orphaned/crashed/timed-out running tasks, then promote."""
    reap_worker_zombies()
    result.reclaimed = _kb.release_stale_claims(conn)
    if reconcile_orphans:
        result.reconciled_orphans = reconcile_orphaned_running(conn)
    result.stale = detect_stale_running(conn, stale_timeout_seconds=stale_timeout_seconds)
    result.crashed = detect_crashed_workers(conn)
    # Side-channel attributes (see detect_crashed_workers); rate-limited tasks
    # went back to ``ready`` and the respawn guard defers them until quota clears.
    result.auto_blocked.extend(getattr(detect_crashed_workers, "_last_auto_blocked", []))
    result.rate_limited.extend(getattr(detect_crashed_workers, "_last_rate_limited", []))
    result.timed_out = enforce_max_runtime(conn)
    result.promoted = _kb.recompute_ready(conn, failure_limit=failure_limit)


def _tick_spawn_budget(
    conn: sqlite3.Connection,
    result: DispatchResult,
    *,
    max_spawn: Optional[int],
    max_in_progress: Optional[int],
    board: Optional[str],
) -> tuple[bool, Optional[int]]:
    """``(may_spawn, spawn_budget)`` for this tick; ``budget None`` = uncapped.

    ``max_spawn`` is a live per-board concurrency cap (running + this tick's
    spawns), not a per-tick budget — a per-tick reading would grow concurrency
    by N every tick. ``max_in_progress`` is a HOST-level cap: running workers on
    every other board count against the same budget, else N boards multiply the
    cap by N — exactly the fan-out the memory-derived default exists to prevent.
    """
    # Count already-running tasks so max_spawn enforces concurrency, not a
    # per-tick budget: "running" tasks stay running until the worker calls
    # kanban_complete/kanban_block or the TTL reclaims them.
    running_count = 0
    spawn_budget: Optional[int] = None
    if max_spawn is not None or max_in_progress is not None:
        running_count = count_running_tasks(conn)

    # Both ready and review loops consume from the same budget.
    if max_spawn is not None:
        if running_count >= max_spawn:
            return False, None
        spawn_budget = max_spawn - running_count

    if max_in_progress is not None:
        total_running = running_count + count_running_tasks_other_boards(board)
        if total_running >= max_in_progress:
            return False, None
        remaining = max_in_progress - total_running
        if spawn_budget is None or spawn_budget > remaining:
            spawn_budget = remaining

    # Memory-pressure guard: a static cap can't see the host's actual state.
    # critical -> spawn nothing this tick; elevated -> at most one new worker.
    # Reclaim/promotion already ran, so bookkeeping stays live; deferred tasks
    # wait for a later tick. "unknown" imposes no restriction.
    pressure = _memory_pressure_level()
    if pressure == "critical":
        result.memory_pressure = pressure
        _kb._log.warning(
            "kanban dispatch: system memory pressure is critical; "
            "spawning no new workers this tick (deferred, not dropped)"
        )
        return False, None
    if pressure == "elevated":
        result.memory_pressure = pressure
        if spawn_budget is None or spawn_budget > 1:
            _kb._log.warning(
                "kanban dispatch: system memory pressure is elevated; "
                "limiting to at most 1 new worker this tick"
            )
            spawn_budget = 1
    return True, spawn_budget


def _lane_rows(conn: sqlite3.Connection, status: str) -> list[sqlite3.Row]:
    """Unclaimed rows of one lane in dispatch order."""
    return conn.execute(
        "SELECT id, assignee FROM tasks "
        f"WHERE status = '{status}' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()


def dispatch_once(conn: sqlite3.Connection, **kwargs) -> DispatchResult:
    return _dispatch_once_locked(conn, **kwargs)


def _dispatch_once_locked(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    reconcile_orphans: bool = True,
) -> DispatchResult:
    """One dispatcher tick: reclaim stale/crashed running tasks, promote
    todo -> ready, then atomically claim each spawnable ready/review row and
    call ``spawn_fn(task, workspace_path, board) -> Optional[int]``, recording
    the PID so later ticks catch crashes before the TTL. Cap semantics:
    :func:`_tick_spawn_budget`."""
    result = DispatchResult()
    _run_reclaim_phase(
        conn, result, stale_timeout_seconds=stale_timeout_seconds,
        failure_limit=failure_limit, reconcile_orphans=reconcile_orphans,
    )
    may_spawn, spawn_budget = _tick_spawn_budget(
        conn, result, max_spawn=max_spawn, max_in_progress=max_in_progress, board=board,
    )
    if not may_spawn:
        return result

    ready_rows = _lane_rows(conn, "ready")
    # Review rows are enumerated up front so the budget split can see whether
    # review work exists at all.
    review_rows = _lane_rows(conn, "review") if review_dispatch_enabled() else []
    # Review-lane reservation: the ready loop runs first and would otherwise
    # consume the ENTIRE shared budget, starving reviews under a sustained ready
    # backlog. When spawnable review work exists and there is any budget, hold
    # one slot back.
    ready_budget = spawn_budget
    if spawn_budget is not None and spawn_budget > 0 and _any_spawnable_review(review_rows):
        ready_budget = max(spawn_budget - 1, 0)
    # Per-profile cap. Deferred tasks go to skipped_per_profile_capped, not
    # skipped_unassigned — "busy, retry later" differs from "needs routing".
    per_profile_cap = max_in_progress_per_profile if (
        # Per-profile concurrency cap (#21582): when set, track how many workers each assignee already has
        # in flight, and refuse to spawn when this would push that assignee past the cap. Prevents fan-out
        # workloads from melting a single profile's local model / API quota / browser pool while leaving
        # other profiles idle.
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    per_profile_running: dict[str, int] = {}
    if per_profile_cap is not None:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL "
            "GROUP BY assignee"
        ):
            per_profile_running[prow["assignee"]] = int(prow["n"])
    lane_kwargs: dict[str, Any] = dict(
        dry_run=dry_run, ttl_seconds=ttl_seconds, board=board,
        failure_limit=failure_limit, spawn_fn=spawn_fn,
        per_profile_cap=per_profile_cap, per_profile_running=per_profile_running,
    )
    default_assignee = _resolve_default_assignee(default_assignee)
    spawned = 0
    for row in ready_rows:
        if ready_budget is not None and spawned >= ready_budget:
            break
        row_assignee = row["assignee"]
        if not row_assignee:
            # Honour kanban.default_assignee so an unassigned task doesn't
            # park in 'ready' forever.
            if not default_assignee or not _apply_default_assignee(
                conn, row["id"], default_assignee, dry_run=dry_run,
            ):
                result.skipped_unassigned.append(row["id"])
                continue
            row_assignee = default_assignee
            result.auto_assigned_default.append(row["id"])
        if _dispatch_lane_task(conn, row, row_assignee, result, lane="ready", **lane_kwargs):
            spawned += 1

    # A review agent (sdlc-review) approves (→ done) or requests changes
    # (→ ready/todo). Review spawns share max_spawn with ready tasks. The loop
    # checks the FULL shared ``spawn_budget`` — the reservation above caps the
    # ready lane, it grants no extra capacity here.
    for row in review_rows:
        if spawn_budget is not None and spawned >= spawn_budget:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        if _dispatch_lane_task(conn, row, row["assignee"], result, lane="review", **lane_kwargs):
            spawned += 1
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


from hermes_cli import kanban_db as _kb  # noqa: E402
'''

# ``gateway/memory_status.py`` at v2026.9.14: the classifier and its tiers.
FIXTURE_MEMORY_STATUS = '''"""Fixture: gateway/memory_status.py at v2026.9.14, the classifier only."""
from __future__ import annotations

from typing import Any, Optional

# Thresholds on system MemAvailable.  ``critical`` doubles as the lifecycle
# ledger's OOM-suspicion heuristic: a level that makes a later unclean death
# "suspected OOM" already warns while the process is alive.
_CRITICAL_AVAILABLE_KIB = 64 * 1024  # < 64 MiB available
_CRITICAL_AVAILABLE_FRACTION = 0.05  # < 5% of MemTotal
_ELEVATED_AVAILABLE_KIB = 128 * 1024  # < 128 MiB available
_ELEVATED_AVAILABLE_FRACTION = 0.15  # < 15% of MemTotal
_PRESSURE_TIERS = (  # order-sensitive: worst first
    ("critical", _CRITICAL_AVAILABLE_KIB, _CRITICAL_AVAILABLE_FRACTION),
    ("elevated", _ELEVATED_AVAILABLE_KIB, _ELEVATED_AVAILABLE_FRACTION),
)


def _nonneg_int(value: Any) -> Optional[int]:
    """Return *value* if it is a non-negative int (bools rejected), else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def classify_pressure(available_kib: Any, total_kib: Any) -> str:
    """``ok``/``elevated``/``critical`` from MemAvailable/MemTotal; ``unknown`` when the
    sample is missing/malformed — "could not read it" must never read as "fine"."""
    available, total = _nonneg_int(available_kib), _nonneg_int(total_kib)
    if available is None:
        return "unknown"
    fraction = available / total if total else None
    for level, kib_floor, frac_floor in _PRESSURE_TIERS:
        if available < kib_floor or (fraction is not None and fraction < frac_floor):
            return level
    return "ok"
'''

# ``hermes_cli/config.py``: the one function the module calls, over a dict the
# drive sets.
FIXTURE_CONFIG = '''"""Fixture: hermes_cli/config.py, load_config_readonly over a dict the drive sets."""
CONFIG: dict = {}


def load_config_readonly():
    return CONFIG
'''

FIXTURE_FILES = {
    "hermes_cli/__init__.py": "",
    "hermes_cli/config.py": FIXTURE_CONFIG,
    applier.DB_RELATIVE: FIXTURE_DB,
    applier.DISPATCH_RELATIVE: FIXTURE_DISPATCH,
    "gateway/__init__.py": "",
    "gateway/memory_status.py": FIXTURE_MEMORY_STATUS,
}


def fixture_sources() -> dict:
    """``{relative path: source}`` of the pristine fixture tree; the tests read it too."""
    return dict(FIXTURE_FILES)


def write_fixture(root: Path) -> None:
    """The pristine fixture tree under ``root``, with the module installed as the image does."""
    for relative, source in FIXTURE_FILES.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    (root / "hermes_cli" / "kanban_admission.py").write_text(
        (HERE / "kanban_admission.py").read_text()
    )


# ---------------------------------------------------------------------------
# The drive, shared by the fixture and the real tree
# ---------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    try:
        with open(f"{admission.PROC_ROOT}/{pid}/{admission.PROC_STATUS_FILE}", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("State:"):
                    return not line.split()[1].startswith(ZOMBIE_STATE)
    except OSError:
        pass
    try:
        out = subprocess.run(
            [*PS_STATE_COMMAND, str(pid)], capture_output=True, text=True, timeout=PS_TIMEOUT_SECONDS,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return True
    return bool(out) and not out.startswith(ZOMBIE_STATE)


def _wait_dead(pid: int) -> bool:
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return not _alive(pid)


def _sample(used_mib: int) -> dict:
    budget_kib = BUDGET_MIB * KIB_PER_MIB
    used_kib = used_mib * KIB_PER_MIB
    return {
        admission.TOTAL_KEY: budget_kib,
        admission.AVAILABLE_KEY: max(0, budget_kib - used_kib),
        admission.USAGE_KEY: used_kib,
        admission.SOURCE_KEY: admission.SOURCE_CGROUP_V1,
    }


class Harness:
    """The engine under test: the fixture's or the real tree's."""

    def __init__(self, name: str, K, KD, KA, configure) -> None:
        self.name = name
        self.K = K
        self.KD = KD
        self.KA = KA
        self._configure = configure
        self._boards = 0
        self.tmp = Path(tempfile.mkdtemp())

    def configure(self, **admission) -> None:
        self._configure({"kanban": {"admission": admission}} if admission else {})

    def use_sample(self, sample) -> None:
        self.KA.container_memory_sample = lambda budget_mib, **kwargs: sample

    def use_upstream_sample(self, sample) -> None:
        self.KD._system_memory_sample = lambda: dict(sample)

    def use_rss(self, sizes_mib: dict) -> None:
        self.KA.process_tree_rss_kib = (
            lambda pid, **kwargs: sizes_mib.get(int(pid), WORKER_UNDER_CAP_MIB) * KIB_PER_MIB
        )

    def restore_rss(self) -> None:
        self.KA.process_tree_rss_kib = self._real_rss

    def remember(self) -> None:
        self._real_rss = self.KA.process_tree_rss_kib

    def board(self):
        self._boards += 1
        return self._connect(self.tmp / f"kanban{self._boards}.db")

    def _connect(self, path):
        raise NotImplementedError

    def new_card(self, conn, title: str, *, assignee=PROFILE, age: int = 0, priority: int = 0) -> str:
        raise NotImplementedError

    def claim(self, conn, task_id: str, pid: int) -> None:
        raise NotImplementedError

    def backdate(self, conn, task_id: str, age: int) -> None:
        if age:
            conn.execute("UPDATE tasks SET created_at = created_at - ? WHERE id = ?", (age, task_id))
            conn.commit()

    def tick(self, conn, *, cap=None, dry_run: bool = False):
        asked: list = []

        def spawn_fn(*args, **kwargs):
            task = kwargs.get("task") or kwargs.get("task_id") or (args[0] if args else None)
            asked.append(getattr(task, "id", task))
            return os.getpid()

        result = self.KD.dispatch_once(conn, spawn_fn=spawn_fn, max_in_progress=cap, dry_run=dry_run)
        return asked, result

    def status(self, conn, task_id: str):
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return row[0] if row else None

    def why(self, conn, result, *task_ids) -> str:
        """What the tick recorded and what the board says, for a failure's detail."""
        parts = [
            f"spawned={[entry[0] for entry in result.spawned]}",
            f"unassigned={result.skipped_unassigned}",
            f"nonspawnable={result.skipped_nonspawnable}",
            f"guarded={result.respawn_guarded}",
            f"auto_blocked={result.auto_blocked}",
            f"pressure={result.memory_pressure}",
            f"admission={getattr(result, self.KA.RESULT_ATTR, None)!r}",
        ]
        for task_id in task_ids:
            events = [
                row[0]
                for row in conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,),
                )
            ]
            parts.append(f"{task_id}: status={self.status(conn, task_id)} events={events}")
        return "; ".join(parts)

    def last_block(self, conn, task_id: str) -> dict:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return json.loads(row[0]) if row and row[0] else {}


class FixtureHarness(Harness):
    def _connect(self, path):
        return self.K.connect(path)

    def new_card(self, conn, title, *, assignee=PROFILE, age=0, priority=0):
        task_id = self.K.create_task(conn, title=title, assignee=assignee, priority=priority)
        self.backdate(conn, task_id, age)
        return task_id

    def claim(self, conn, task_id, pid):
        if self.K.claim_task(conn, task_id) is None:
            raise SystemExit(f"{self.name}: fixture defect, {task_id} did not claim")
        conn.execute("UPDATE tasks SET worker_pid = ? WHERE id = ?", (pid, task_id))
        conn.commit()


class RealHarness(Harness):
    def __init__(self, K, KC, KD, KA, configure) -> None:
        super().__init__("real tree", K, KD, KA, configure)
        self.KC = KC

    def _connect(self, path):
        conn = self.KC.connect(path)
        leftovers = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
        if leftovers:
            raise SystemExit(f"{self.name}: fixture defect, a fresh board holds {leftovers} card(s)")
        return conn

    def new_card(self, conn, title, *, assignee=PROFILE, age=0, priority=0):
        task = self.K.create_task(conn, title=title, assignee=assignee, priority=priority)
        task_id = task.id if hasattr(task, "id") else str(task)
        self.K.recompute_ready(conn)
        self.backdate(conn, task_id, age)
        return task_id

    def claim(self, conn, task_id, pid):
        if self.K.claim_task(conn, task_id) is None:
            raise SystemExit(f"{self.name}: fixture defect, {task_id} did not claim")
        self.KD._set_worker_pid(conn, task_id, pid)


def drive(h: Harness) -> None:
    """Every behaviour the patch promises, through ``dispatch_once``."""
    KA = h.KA
    h.remember()
    print(f"{h.name}: admission through dispatch_once")

    # A. The budget decides, not /proc/meminfo.
    h.configure(memory_budget_mib=BUDGET_MIB)
    h.use_upstream_sample(PLENTIFUL_SAMPLE)
    h.use_sample(_sample(USAGE_CRITICAL_MIB))
    conn = h.board()
    first = h.new_card(conn, "Fleet readiness report")
    second = h.new_card(conn, "Triage a cluster event")
    asked, res = h.tick(conn)
    check("A1. a critical budget sample spawns nothing, whatever meminfo says",
          asked == [] and res.memory_pressure == "critical", f"spawned {asked}, pressure {res.memory_pressure}")
    record = getattr(res, KA.RESULT_ATTR, {})
    check("A2. the tick's record names the level, the figures and the counter",
          record.get("level") == "critical" and record.get("limited") is True
          and record.get("usage_mib") == USAGE_CRITICAL_MIB and record.get("budget_mib") == BUDGET_MIB
          and record.get("source") == admission.SOURCE_CGROUP_V1, repr(record))
    check("A3. fresh cards stay ready under pressure (deferred, not refused)",
          h.status(conn, first) == "ready" and h.status(conn, second) == "ready")
    h.use_sample(_sample(USAGE_ELEVATED_MIB))
    asked, res = h.tick(conn)
    check("A4. an elevated budget sample spawns at most one",
          len(asked) == 1 and res.memory_pressure == "elevated", h.why(conn, res, first, second))
    h.use_sample(_sample(USAGE_OK_MIB))
    asked, res = h.tick(conn)
    check("A5. an ok budget sample spawns the rest",
          len(asked) == 1 and res.memory_pressure is None, h.why(conn, res, first, second))
    # The headroom cap: elevated with less than one worker reserve free spawns nothing,
    # whatever upstream's "at most one" would allow, and the tick counts as limited.
    h.configure(memory_budget_mib=SMALL_BUDGET_MIB)
    h.use_sample({
        admission.TOTAL_KEY: SMALL_BUDGET_MIB * KIB_PER_MIB,
        admission.AVAILABLE_KEY: (SMALL_BUDGET_MIB - SMALL_USAGE_MIB) * KIB_PER_MIB,
        admission.USAGE_KEY: SMALL_USAGE_MIB * KIB_PER_MIB,
        admission.SOURCE_KEY: admission.SOURCE_CGROUP_V1,
    })
    third = h.new_card(conn, "A third card under a small budget")
    asked, res = h.tick(conn)
    record = getattr(res, KA.RESULT_ATTR, {})
    check("A6. elevated with under one reserve of headroom spawns nothing and is limited",
          asked == [] and res.memory_pressure == "elevated" and record.get("spawn_cap") == 0
          and record.get("limited") is True and h.status(conn, third) == "ready",
          f"spawned {asked}, pressure {res.memory_pressure}, record {record!r}")
    h.configure(memory_budget_mib=BUDGET_MIB)
    conn.close()

    # B. Unreadable counters fail closed, or fall back when told to.
    h.use_sample(None)
    conn = h.board()
    question = h.new_card(conn, "Question")
    asked, res = h.tick(conn)
    record = getattr(res, KA.RESULT_ATTR, {})
    check("B1. unreadable counters spawn nothing while fail_closed is on",
          asked == [] and res.memory_pressure == "critical" and record.get("fail_closed") is True,
          h.why(conn, res, question))
    h.configure(memory_budget_mib=BUDGET_MIB, fail_closed=False)
    asked, res = h.tick(conn)
    check("B2. with fail_closed off, unreadable counters fall back to upstream's reading",
          len(asked) == 1 and res.memory_pressure is None, h.why(conn, res, question))
    conn.close()

    # C. No budget: upstream decides, and its unknown still fails closed.
    h.configure()
    h.use_upstream_sample(PLENTIFUL_SAMPLE)
    conn = h.board()
    question = h.new_card(conn, "Question")
    asked, res = h.tick(conn)
    check("C1. without a budget an idle meminfo spawns", len(asked) == 1, h.why(conn, res, question))
    h.use_upstream_sample({})
    another = h.new_card(conn, "Another question")
    asked, res = h.tick(conn)
    record = getattr(res, KA.RESULT_ATTR, {})
    check("C2. without a budget, upstream's unknown fails closed",
          asked == [] and res.memory_pressure == "critical" and record.get("fail_closed") is True,
          h.why(conn, res, another))
    conn.close()
    h.use_upstream_sample(PLENTIFUL_SAMPLE)

    # D. A worker over its RSS cap is stopped and its card refused.
    h.configure(memory_budget_mib=BUDGET_MIB, worker_rss_cap_mib=WORKER_CAP_MIB)
    h.use_sample(_sample(USAGE_OK_MIB))
    conn = h.board()
    heavy = h.new_card(conn, "Fleet-wide audit")
    light = h.new_card(conn, "Small question")
    sleeper = subprocess.Popen(["sleep", SLEEP_SECONDS])
    try:
        h.claim(conn, heavy, sleeper.pid)
        h.claim(conn, light, os.getpid())
        h.use_rss({sleeper.pid: WORKER_OVER_CAP_MIB})
        asked, res = h.tick(conn)
        payload = h.last_block(conn, heavy)
        expected = KA.capacity_reason(
            f"the worker used {WORKER_OVER_CAP_MIB} MiB against the {WORKER_CAP_MIB} MiB cap"
        )
        check("D1. the oversized worker's card is blocked with kind capacity",
              h.status(conn, heavy) == "blocked" and payload.get("kind") == "capacity",
              f"status {h.status(conn, heavy)}, payload {payload!r}")
        check("D2. the reason carries the figures and the options", payload.get("reason") == expected,
              repr(payload.get("reason")))
        check("D3. the id joins result.auto_blocked", heavy in res.auto_blocked, repr(res.auto_blocked))
        check("D4. the worker process is gone", _wait_dead(sleeper.pid))
        check("D5. a worker under the cap keeps running", h.status(conn, light) == "running",
              h.status(conn, light))
    finally:
        try:
            sleeper.kill()
            sleeper.wait(timeout=WAIT_SECONDS)
        except Exception:  # noqa: BLE001 - already reaped or gone
            pass
        h.restore_rss()
    conn.close()

    # E. A ready card past the bound is refused when the slot cap is full.
    h.configure(memory_budget_mib=BUDGET_MIB, queue_bound_seconds=QUEUE_BOUND_SECONDS)
    conn = h.board()
    busy = h.new_card(conn, "Long audit")
    h.claim(conn, busy, os.getpid())
    old = h.new_card(conn, "Old question", age=OLD_AGE_SECONDS)
    fresh = h.new_card(conn, "Fresh question")
    asked, res = h.tick(conn, cap=1, dry_run=True)
    check("E1. a dry run refuses nothing", asked == [] and h.status(conn, old) == "ready",
          f"spawned {asked}, old {h.status(conn, old)}")
    asked, res = h.tick(conn, cap=1)
    payload = h.last_block(conn, old)
    check("E2. at a full cap the card past the bound is blocked with kind capacity",
          asked == [] and h.status(conn, old) == "blocked" and payload.get("kind") == "capacity",
          f"payload {payload!r}; " + h.why(conn, res, busy, old))
    reason = payload.get("reason") or ""
    check("E3. the reason names the wait and the bound",
          reason.startswith("capacity: the card waited ") and f"against the {QUEUE_BOUND_SECONDS} s bound" in reason
          and reason.endswith(KA.REASON_OPTIONS), repr(reason))
    check("E4. the fresh card keeps waiting", h.status(conn, fresh) == "ready", h.status(conn, fresh))
    check("E5. the refused id joins result.auto_blocked", old in res.auto_blocked, repr(res.auto_blocked))
    conn.close()

    # F. The final-return path: memory leaves a card waiting; the exclusions
    # hold for a row the loop reached and for one behind the exhausted budget.
    h.use_sample(_sample(USAGE_ELEVATED_MIB))
    conn = h.board()
    unrouted_first = h.new_card(conn, "Unrouted card the loop reaches", assignee=None, age=OLDEST_AGE_SECONDS)
    oldest = h.new_card(conn, "First old question", age=OLDER_AGE_SECONDS)
    older = h.new_card(conn, "Second old question", age=OLD_AGE_SECONDS)
    unrouted_last = h.new_card(conn, "Unrouted card behind the budget", assignee=None, age=YOUNG_AGE_SECONDS)
    asked, res = h.tick(conn)
    check("F1. elevated memory spawns the oldest routed card", asked == [oldest], h.why(conn, res, oldest, older))
    check("F2. the card it left waiting past the bound is refused",
          h.status(conn, older) == "blocked" and h.last_block(conn, older).get("kind") == "capacity",
          h.status(conn, older))
    check("F3. a card the loop skipped as unassigned is never refused",
          h.status(conn, unrouted_first) == "ready" and unrouted_first in res.skipped_unassigned,
          f"status {h.status(conn, unrouted_first)}, skipped {res.skipped_unassigned}")
    check("F4. an unassigned card behind the exhausted budget is never refused either",
          h.status(conn, unrouted_last) == "ready" and unrouted_last not in res.skipped_unassigned,
          f"status {h.status(conn, unrouted_last)}, skipped {res.skipped_unassigned}")
    conn.close()

    # G. An exhausted slot budget counts; a tick that is not limited refuses nothing.
    h.use_sample(_sample(USAGE_OK_MIB))
    conn = h.board()
    busy = h.new_card(conn, "Running audit")
    h.claim(conn, busy, os.getpid())
    first = h.new_card(conn, "User question one", age=OLD_AGE_SECONDS, priority=USER_PRIORITY)
    second = h.new_card(conn, "User question two", age=YOUNG_AGE_SECONDS, priority=USER_PRIORITY)
    asked, res = h.tick(conn, cap=2)
    check("G1. one slot left: the first card spawns, the second past the bound is refused",
          asked == [first] and h.status(conn, second) == "blocked"
          and h.last_block(conn, second).get("kind") == "capacity",
          h.why(conn, res, busy, first, second))
    conn.close()
    conn = h.board()
    old = h.new_card(conn, "Old question, idle install", age=OLD_AGE_SECONDS)
    asked, res = h.tick(conn)
    check("G2. an unlimited tick spawns the old card instead of refusing it",
          asked == [old] and h.status(conn, old) == "running", h.why(conn, res, old))
    conn.close()


# ---------------------------------------------------------------------------
# The two halves
# ---------------------------------------------------------------------------


def _inserted_statements(tree: ast.Module) -> list:
    """The statements the applier inserted: those that call through the alias."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Expr, ast.Assign)):
            continue
        if any(
            isinstance(name, ast.Name) and name.id == applier.MODULE_ALIAS
            for name in ast.walk(node)
        ):
            found.append(node)
    return found


def fixture_half() -> None:
    print("fixture tree:")
    root = Path(tempfile.mkdtemp())
    write_fixture(root)
    edited = applier.apply(root)
    check("applied to the fixture", edited is True)
    dispatch = (root / applier.DISPATCH_RELATIVE).read_text()
    db = (root / applier.DB_RELATIVE).read_text()
    for marker in applier.DISPATCH_MARKERS:
        check(f"dispatcher carries {marker!r}", marker in dispatch)
    check("the refusal is called at both returns", dispatch.count(applier.REFUSE_BUILD_MARKER) == 2,
          str(dispatch.count(applier.REFUSE_BUILD_MARKER)))
    check("kanban_db carries the capacity kind", applier.KINDS_BUILD_MARKER in db)
    check("the fanout reaper's anchor is left exactly once", dispatch.count(FANOUT_REAPER_ANCHOR) == 1,
          str(dispatch.count(FANOUT_REAPER_ANCHOR)))
    tree = ast.parse(dispatch)
    inserted = _inserted_statements(tree)
    check("five statements call through the alias", len(inserted) == 5, str(len(inserted)))
    for node in inserted:
        names = patchlib.unbound(tree, node)
        check(f"line {node.lineno} reads only bound names", not names, f"unbound {names}")
    before = {relative: (root / relative).read_text() for relative in (applier.DISPATCH_RELATIVE, applier.DB_RELATIVE)}
    edited_again = applier.apply(root)
    after = {relative: (root / relative).read_text() for relative in before}
    check("a second apply is a no-op", edited_again is False and before == after)
    if FAILURES:
        return
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root)
    run = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), DRIVE_FLAG, str(root)],
        cwd=str(root), env=env, capture_output=True, text=True,
    )
    sys.stdout.write(run.stdout)
    if run.returncode != 0:
        sys.stdout.write(run.stderr)
        FAILURES.append(f"the fixture drive exited {run.returncode}")


def fixture_drive(root: Path) -> int:
    sys.path.insert(0, str(root))
    from hermes_cli import config as CFG
    from hermes_cli import kanban_admission as KA
    from hermes_cli import kanban_db as K
    from hermes_cli import kanban_db_dispatch as KD

    def configure(config: dict) -> None:
        CFG.CONFIG = config

    drive(FixtureHarness("fixture", K, KD, KA, configure))
    if FAILURES:
        print(f"fixture drive: {len(FAILURES)} FAILED")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    return 0


def real_half() -> None:
    installed = HERMES / "hermes_cli" / "kanban_admission.py"
    if not installed.is_file():
        print(f"real tree: {installed} is not there; the fixture half is the whole run")
        return
    print("real tree:")
    if str(HERMES) not in sys.path:
        sys.path.insert(0, str(HERMES))
    from hermes_cli import kanban_admission as KA
    from hermes_cli import kanban_db as K
    from hermes_cli import kanban_db_connect as KC
    from hermes_cli import kanban_db_dispatch as KD
    from hermes_cli import profiles as _profiles

    # The image has no profile directories at build time (the operator
    # scaffolds them at pod start), so every assignee would be dropped as
    # non-spawnable before a slot or the budget was consulted. Make the one
    # profile the drive assigns to exist, as verify_kanban_scheduling does;
    # ``_profile_exists_fn`` imports the name at call time, so the stub holds.
    _profiles.profile_exists = lambda name: name == PROFILE

    check("the capacity kind is a valid block kind", KA.CAPACITY_KIND in K.VALID_BLOCK_KINDS,
          repr(sorted(K.VALID_BLOCK_KINDS)))
    check("the dispatcher binds the module through its trailer",
          getattr(KD, applier.MODULE_ALIAS, None) is KA)
    dispatch = (HERMES / applier.DISPATCH_RELATIVE).read_text()
    for marker in applier.DISPATCH_MARKERS:
        check(f"the installed dispatcher carries {marker!r}", marker in dispatch)

    def configure(config: dict) -> None:
        KA._ENGINE.config = lambda: config

    drive(RealHarness(K, KC, KD, KA, configure))


def main(argv: list) -> int:
    if len(argv) >= 3 and argv[1] == DRIVE_FLAG:
        return fixture_drive(Path(argv[2]))
    fixture_half()
    if not FAILURES:
        real_half()
    print()
    if FAILURES:
        print(f"verify_kanban_admission: {len(FAILURES)} FAILED")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("verify_kanban_admission: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
