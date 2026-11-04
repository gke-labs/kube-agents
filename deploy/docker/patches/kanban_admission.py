"""Admit kanban workers by memory and refuse work with a ``capacity`` reason.

Why
---
The Platform Agent's gateway container runs the dispatcher and every worker it
spawns. Its limits were counts: ``kanban.max_in_progress`` slots, and
upstream's memory guard in ``_tick_spawn_budget``, which classifies
``/proc/meminfo`` (``MemAvailable`` against ``MemTotal``). Under GKE Sandbox
(gVisor) on Autopilot that file reports the sandbox's virtual memory (10.25 GiB
on the development install), not the container's 8 GiB limit or its 2 GiB
request, and the cgroup v2 files the ``cgroup-memory-sample`` patch reads are
absent. The guard never fired, and the kubelet evicted the pod three times at
3.1 to 3.5 GiB of use against the 2 GiB request (gke-labs/kube-agents#4760).
Inside the sandbox, cgroup v1 ``memory.usage_in_bytes`` tracks ``kubectl top``
within 2%.

What
----
Three admission decisions, each a function the dispatcher patch
(``apply_kanban_admission.py``) calls from the tick:

* :func:`pressure_level_for_tick` classifies the container's current memory
  against the operator's budget (``kanban.admission.memory_budget_mib``, the
  container's memory request) with upstream's own
  ``gateway.memory_status.classify_pressure``, so ``elevated`` (at most one
  spawn this tick) and ``critical`` (no spawn) keep their meaning. Counters
  that cannot be read fail closed: the tick spawns nothing and logs why,
  instead of upstream's "unknown imposes no restriction".
* :func:`enforce_worker_rss_caps` stops a running worker whose process tree is
  over ``worker_rss_cap_mib`` (SIGTERM, grace, SIGKILL, then the descendants
  that outlived it) and blocks its card with kind ``capacity``.
* :func:`refuse_overdue_ready` blocks a ready card that has waited past
  ``queue_bound_seconds`` for a slot the tick could not give it, for memory or
  for an exhausted slot budget, with the same kind. Never a card the tick
  skipped for a reason of its own: unassigned, not a profile, respawn-guarded,
  per-profile capped.

A ``capacity`` block carries one sentence of cause and the options, so the
front door can answer with the alternatives instead of retrying. Every log
line starts with ``kanban admission:``. No decision raises into the tick: a
failure is logged, and the pressure check fails closed.

Settings are ``kanban.admission.*`` in the Hermes config. The operator renders
all four keys; the defaults below apply when a key is missing or malformed.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)

#: Every log line this module writes starts with this.
LOG_PREFIX = "kanban admission:"

#: The settings' path in the Hermes config: ``kanban.admission.<key>``.
CONFIG_SECTION = "kanban"
ADMISSION_SECTION = "admission"
MEMORY_BUDGET_KEY = "memory_budget_mib"
WORKER_RSS_CAP_KEY = "worker_rss_cap_mib"
QUEUE_BOUND_KEY = "queue_bound_seconds"
FAIL_CLOSED_KEY = "fail_closed"

#: Defaults for a missing or malformed key. The budget has none: without one
#: the tick keeps upstream's classifier over ``/proc/meminfo``.
DEFAULT_WORKER_RSS_CAP_MIB = 1536
DEFAULT_QUEUE_BOUND_SECONDS = 120
DEFAULT_FAIL_CLOSED = True

#: The words a YAML or environment value may spell a boolean with.
TRUE_WORDS = ("true", "yes", "on", "1")
FALSE_WORDS = ("false", "no", "off", "0")

#: The block kind this patch adds to ``kanban_db.VALID_BLOCK_KINDS``.
CAPACITY_KIND = "capacity"

#: The container's memory counters, tried in this order: cgroup v1 is what
#: gVisor exposes, cgroup v2 what a plain node exposes, and the dispatcher's
#: own process tree is the fallback when neither is mounted.
CGROUP_V1_USAGE_PATH = "/sys/fs/cgroup/memory/memory.usage_in_bytes"
CGROUP_V2_USAGE_PATH = "/sys/fs/cgroup/memory.current"
#: Beside each usage counter, the cgroup's ``memory.stat``. A usage counter
#: includes the file cache, which reclaim frees before anything is evicted;
#: the kubelet evicts on the working set, usage less the inactive file cache,
#: so the sampler subtracts the same key and reads as the kubelet does. A
#: missing file or key subtracts nothing.
CGROUP_V1_STAT_PATH = "/sys/fs/cgroup/memory/memory.stat"
CGROUP_V2_STAT_PATH = "/sys/fs/cgroup/memory.stat"
CGROUP_V1_INACTIVE_FILE_KEY = "total_inactive_file"
CGROUP_V2_INACTIVE_FILE_KEY = "inactive_file"
PROC_ROOT = "/proc"
SOURCE_CGROUP_V1 = "cgroup-v1"
SOURCE_CGROUP_V2 = "cgroup-v2"
SOURCE_PROCESS_TREE = "process-tree"
SOURCE_UPSTREAM = "meminfo"

#: ``/proc/<pid>/status`` carries the resident set as ``VmRSS:  <n> kB``.
#: ``/proc/<pid>/stat`` carries, after the parenthesised command, the state,
#: then the parent pid (the file's field 4), the process group (field 5) and
#: the start time (field 22).
PROC_STATUS_FILE = "status"
PROC_STAT_FILE = "stat"
VMRSS_FIELD = "VmRSS:"
STAT_PPID_INDEX = 1
STAT_PGRP_INDEX = 2
STAT_STARTTIME_INDEX = 19

BYTES_PER_KIB = 1024
KIB_PER_MIB = 1024

#: Upstream's sample keys (``gateway.lifecycle_ledger.sample_memory``), which
#: ``classify_pressure`` reads, plus two this module adds for its log line.
TOTAL_KEY = "mem_total_kib"
AVAILABLE_KEY = "mem_available_kib"
USAGE_KEY = "usage_kib"
INACTIVE_FILE_KEY = "inactive_file_kib"
SOURCE_KEY = "source"

#: Upstream's levels (``gateway.memory_status.classify_pressure``).
LEVEL_OK = "ok"
LEVEL_ELEVATED = "elevated"
LEVEL_CRITICAL = "critical"
LEVEL_UNKNOWN = "unknown"
LIMITING_LEVELS = (LEVEL_ELEVATED, LEVEL_CRITICAL)

#: What this module records on the tick's ``DispatchResult``: a dict with the
#: level, whether the tick is limited, the figures, and the ids it blocked.
RESULT_ATTR = "kanban_admission"
RECORD_LIMITED = "limited"
RECORD_LEVEL = "level"
RECORD_CAUSE = "cause"
RECORD_FAIL_CLOSED = "fail_closed"
RECORD_BUDGET_MIB = "budget_mib"
RECORD_USAGE_MIB = "usage_mib"
RECORD_SOURCE = "source"
RECORD_REFUSED = "refused"
RECORD_SPAWN_CAP = "spawn_cap"
#: What one new worker is reserved at when the tick's spawns are bounded by the
#: budget's headroom: upstream's MEMORY_GUARD_MB_PER_WORKER, the figure its
#: memory-derived default cap divides by, and about one measured idle worker.
WORKER_RESERVE_MIB = 512

#: ``DispatchResult`` fields whose ids a tick skipped for a reason of its own,
#: so they are never refused for capacity: plain id lists, and lists of tuples
#: whose first element is the id (upstream's ``respawn_guarded``,
#: ``skipped_per_profile_capped`` and ``spawned`` shapes). The lists only name
#: the rows the ready loop reached, so a row behind an exhausted budget is
#: asked the same questions again before it is refused. ``queued_noticed`` is
#: the sibling kanban_priority patch's list of the cards it told they are
#: queued this tick: at the end of a tick that notice has already been posted,
#: so a card on it is refused no earlier than the next limited tick and its
#: thread reads the queued line before the refusal.
EXCLUDED_ID_FIELDS = ("skipped_unassigned", "skipped_nonspawnable", "queued_noticed")
EXCLUDED_TUPLE_FIELDS = ("respawn_guarded", "skipped_per_profile_capped", "spawned")
#: The lane the respawn guard is asked about.
READY_LANE = "ready"
#: Upstream's list of ids the tick blocked, which the two refusals extend.
AUTO_BLOCKED_FIELD = "auto_blocked"
#: The sibling kanban_priority patch's field: ready rows its per-class slot
#: reservation held back this tick. Read here, never written.
RESERVED_FIELD = "skipped_reserved"

#: The keys of the dict ``_stop_worker`` returns, and the two it reads from
#: upstream's ``_terminate_reclaimed_worker`` result.
OUTCOME_PID = "pid"
OUTCOME_TERMINATED = "terminated"
OUTCOME_SIGKILL = "sigkill"
OUTCOME_ORPHANS = "orphans_killed"
OUTCOME_GROUP = "group_killed"
OUTCOME_TERMINATION = "termination"
TERMINATION_TERMINATED_KEY = "terminated"
TERMINATION_SIGKILL_KEY = "sigkill"

#: The reason text: ``capacity: <cause>; <tail> <options>``.
SCOPE_CLUSTERS = 10
REASON_TAIL = "the install cannot take this now."
REASON_OPTIONS = (
    "Options: wait for the queue to drain, ask for a smaller scope (at most "
    f"{SCOPE_CLUSTERS} clusters), or use the scheduled report."
)

#: Events that return a card to the ready lane without closing a run, each
#: written by the pinned release: a promotion (``recompute_ready``, or by
#: hand), an unblock, an assignment, a reviewer's changes request, a review
#: reopened and a reclaim. With the card's creation and its last run's end,
#: they bound how long it has waited.
READY_ENTRY_EVENT_KINDS = (
    "promoted", "promoted_manual", "unblocked", "assigned",
    "changes_requested", "review_reopened", "reclaimed",
)

_READY_ENTRY_PLACEHOLDERS = ", ".join("?" for _ in READY_ENTRY_EVENT_KINDS)

#: Unclaimed ready rows with the instant each one last entered the lane.
#: sqlite's multi-argument MAX is NULL when any argument is, hence COALESCE.
_OVERDUE_SQL = (
    "SELECT t.id, t.assignee, MAX(t.created_at, "
    "COALESCE((SELECT MAX(r.ended_at) FROM task_runs r WHERE r.task_id = t.id), 0), "
    "COALESCE((SELECT MAX(e.created_at) FROM task_events e WHERE e.task_id = t.id "
    f"AND e.kind IN ({_READY_ENTRY_PLACEHOLDERS})), 0)) AS waiting_since "
    "FROM tasks t WHERE t.status = 'ready' AND t.claim_lock IS NULL "
    "ORDER BY t.created_at ASC"
)

#: Running rows with a worker to measure.
_RUNNING_SQL = (
    "SELECT id, worker_pid, claim_lock, current_run_id FROM tasks "
    "WHERE status = 'running' AND worker_pid IS NOT NULL"
)


# ---------------------------------------------------------------------------
# The engine seam
# ---------------------------------------------------------------------------


class _HermesEngine:
    """The engine helpers the decisions need, resolved on first use.

    ``kanban_db_dispatch`` imports this module at its tail, so importing it
    here at module level would be circular; and the unit tests run this file
    flat, without ``hermes_cli`` at all, and pass a fake in its place.
    """

    def __init__(self) -> None:
        self._db = None
        self._dispatch = None

    @property
    def db(self):
        if self._db is None:
            from hermes_cli import kanban_db

            self._db = kanban_db
        return self._db

    @property
    def dispatch(self):
        if self._dispatch is None:
            from hermes_cli import kanban_db_dispatch

            self._dispatch = kanban_db_dispatch
        return self._dispatch

    def host_prefix(self) -> str:
        return self.db._host_prefix()

    def pid_alive(self, pid: int) -> bool:
        return bool(self.dispatch._pid_alive(pid))

    def kill_fn(self, signal_fn) -> Optional[Callable[[int, int], None]]:
        return self.dispatch._kill_fn(signal_fn)

    def sigkill(self, kill, pid: int) -> bool:
        return bool(self.dispatch._sigkill(kill, pid))

    def terminate(self, pid: int, claim_lock: str, signal_fn) -> dict:
        """Upstream's kill ladder: SIGTERM, a short grace, SIGKILL; ``terminated`` says whether it worked."""
        return dict(self.dispatch._terminate_reclaimed_worker(pid, claim_lock, signal_fn=signal_fn))

    def survived(self, termination: Mapping[str, Any]) -> bool:
        return bool(self.dispatch._worker_survived_termination(termination))

    def block_task(self, conn, task_id: str, *, reason: str, kind: str, expected_run_id=None) -> bool:
        return bool(
            self.db.block_task(conn, task_id, reason=reason, kind=kind, expected_run_id=expected_run_id)
        )

    def upstream_pressure_level(self) -> str:
        return str(self.dispatch._memory_pressure_level())

    def profile_exists(self, assignee: str) -> Optional[bool]:
        """Whether ``assignee`` names a profile; None when the engine cannot tell."""
        exists = self.dispatch._profile_exists_fn()
        return None if exists is None else bool(exists(assignee))

    def respawn_guard_reason(self, conn, task_id: str) -> Optional[str]:
        return self.dispatch.check_respawn_guard(conn, task_id, lane=READY_LANE)

    def classify(self, available_kib: Any, total_kib: Any) -> str:
        from gateway.memory_status import classify_pressure

        return str(classify_pressure(available_kib, total_kib))

    def config(self) -> Mapping[str, Any]:
        from hermes_cli.config import load_config_readonly

        return load_config_readonly() or {}


_ENGINE = _HermesEngine()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionSettings:
    """``kanban.admission`` as the tick reads it."""

    memory_budget_mib: Optional[int] = None
    worker_rss_cap_mib: int = DEFAULT_WORKER_RSS_CAP_MIB
    queue_bound_seconds: int = DEFAULT_QUEUE_BOUND_SECONDS
    fail_closed: bool = DEFAULT_FAIL_CLOSED


def _positive_int(value: Any) -> Optional[int]:
    """``value`` as an int above zero, else None. A bool or a fraction is not a count."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        word = value.strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
    return default


def _section(config: Any, key: str) -> Mapping[str, Any]:
    value = config.get(key) if isinstance(config, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def settings_from(config: Any) -> AdmissionSettings:
    """The settings in a loaded Hermes config; defaults for whatever is missing or malformed."""
    admission = _section(_section(config, CONFIG_SECTION), ADMISSION_SECTION)
    return AdmissionSettings(
        memory_budget_mib=_positive_int(admission.get(MEMORY_BUDGET_KEY)),
        worker_rss_cap_mib=_positive_int(admission.get(WORKER_RSS_CAP_KEY)) or DEFAULT_WORKER_RSS_CAP_MIB,
        queue_bound_seconds=_positive_int(admission.get(QUEUE_BOUND_KEY)) or DEFAULT_QUEUE_BOUND_SECONDS,
        fail_closed=_as_bool(admission.get(FAIL_CLOSED_KEY), DEFAULT_FAIL_CLOSED),
    )


def load_settings(config: Any = None, *, engine=None) -> AdmissionSettings:
    """The settings from ``hermes_cli.config.load_config_readonly()``, or from ``config``.

    The loaded dict is Hermes' in-process cache and is only read here.
    """
    if config is None:
        try:
            config = (engine or _ENGINE).config()
        except Exception as exc:  # noqa: BLE001 - a tick never dies on config
            logger.warning("%s the Hermes config is unreadable (%r); using the defaults", LOG_PREFIX, exc)
            config = {}
    return settings_from(config)


# ---------------------------------------------------------------------------
# Memory counters
# ---------------------------------------------------------------------------


def _read_int_file(path: str) -> Optional[int]:
    try:
        with open(path, encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _read_stat_key(path: str, key: str) -> Optional[int]:
    """The value of ``key`` in a ``<key> <value>`` file such as ``memory.stat``."""
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                name, _, value = line.partition(" ")
                if name == key:
                    return int(value.strip())
    except (OSError, ValueError):
        return None
    return None


def _proc_path(proc_root: str, pid: int, name: str) -> str:
    return os.path.join(proc_root, str(int(pid)), name)


def _vmrss_kib(proc_root: str, pid: int) -> Optional[int]:
    """The resident set of one process in KiB; None when it has none or is gone."""
    try:
        with open(_proc_path(proc_root, pid, PROC_STATUS_FILE), encoding="utf-8") as fh:
            for line in fh:
                if line.startswith(VMRSS_FIELD):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _stat_fields(proc_root: str, pid: int) -> Optional[list]:
    """The fields of ``/proc/<pid>/stat`` after the parenthesised command, or None."""
    try:
        with open(_proc_path(proc_root, pid, PROC_STAT_FILE), encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    _, separator, rest = text.rpartition(")")
    if not separator:
        return None
    return rest.split()


def _stat_field(proc_root: str, pid: int, index: int) -> Optional[str]:
    fields = _stat_fields(proc_root, pid)
    if fields is None or len(fields) <= index:
        return None
    return fields[index]


def _children_map(proc_root: str) -> dict:
    """``parent pid -> [child pids]`` over everything under ``proc_root``."""
    children: dict = {}
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return children
    for entry in entries:
        if not entry.isdigit():
            continue
        parent = _stat_field(proc_root, int(entry), STAT_PPID_INDEX)
        if parent is None or not parent.isdigit():
            continue
        children.setdefault(int(parent), []).append(int(entry))
    return children


def process_tree(pid: int, *, proc_root: str = PROC_ROOT) -> list:
    """``pid`` and every descendant that exists now, parents before children."""
    children = _children_map(proc_root)
    ordered: list = []
    seen: set = set()
    stack = [int(pid)]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        ordered.append(current)
        stack.extend(children.get(current, ()))
    return ordered


def process_tree_rss_kib(pid: int, *, proc_root: str = PROC_ROOT) -> Optional[int]:
    """The resident set of ``pid``'s process tree in KiB; None when ``pid`` is gone."""
    own = _vmrss_kib(proc_root, pid)
    if own is None:
        return None
    total = own
    for member in process_tree(pid, proc_root=proc_root)[1:]:
        total += _vmrss_kib(proc_root, member) or 0
    return total


def container_memory_sample(
    budget_mib: int,
    *,
    cgroup_v1_path: str = CGROUP_V1_USAGE_PATH,
    cgroup_v2_path: str = CGROUP_V2_USAGE_PATH,
    cgroup_v1_stat_path: str = CGROUP_V1_STAT_PATH,
    cgroup_v2_stat_path: str = CGROUP_V2_STAT_PATH,
    proc_root: str = PROC_ROOT,
    own_pid: Optional[int] = None,
) -> Optional[dict]:
    """The container's memory in upstream's sample shape, against ``budget_mib``.

    ``mem_total_kib`` is the budget and ``mem_available_kib`` the budget less
    the container's working set, so ``classify_pressure`` reads the budget the
    way it reads a host's ``MemTotal``. The working set is the cgroup's usage
    less its inactive file cache, the kubelet's eviction figure; a process
    tree's resident set has no cache to subtract. ``None`` when no counter can
    be read.
    """
    budget_kib = int(budget_mib) * KIB_PER_MIB
    usage_kib = None
    inactive_file_kib = 0
    source = None
    counters = (
        (cgroup_v1_path, cgroup_v1_stat_path, CGROUP_V1_INACTIVE_FILE_KEY, SOURCE_CGROUP_V1),
        (cgroup_v2_path, cgroup_v2_stat_path, CGROUP_V2_INACTIVE_FILE_KEY, SOURCE_CGROUP_V2),
    )
    for path, stat_path, inactive_key, name in counters:
        usage = _read_int_file(path)
        if usage is not None and usage >= 0:
            inactive_file = _read_stat_key(stat_path, inactive_key) or 0
            inactive_file_kib = max(0, inactive_file) // BYTES_PER_KIB
            usage_kib = max(0, usage // BYTES_PER_KIB - inactive_file_kib)
            source = name
            break
    if usage_kib is None:
        usage_kib = process_tree_rss_kib(os.getpid() if own_pid is None else own_pid, proc_root=proc_root)
        source = SOURCE_PROCESS_TREE
    if usage_kib is None:
        return None
    return {
        TOTAL_KEY: budget_kib,
        AVAILABLE_KEY: max(0, budget_kib - usage_kib),
        USAGE_KEY: usage_kib,
        INACTIVE_FILE_KEY: inactive_file_kib,
        SOURCE_KEY: source,
    }


# ---------------------------------------------------------------------------
# The three decisions
# ---------------------------------------------------------------------------


def _record(result: Any, **fields: Any) -> dict:
    """Merge ``fields`` into this module's record on the tick's result."""
    record = getattr(result, RESULT_ATTR, None)
    if not isinstance(record, dict):
        record = {}
        try:
            setattr(result, RESULT_ATTR, record)
        except Exception:  # noqa: BLE001 - a result that refuses attributes still gets a decision
            pass
    record.update(fields)
    return record


def capacity_reason(cause: str) -> str:
    """One sentence of cause, then the options the front door can offer."""
    return f"{CAPACITY_KIND}: {cause}; {REASON_TAIL} {REASON_OPTIONS}"


def pressure_level_for_tick(result: Any, *, settings: Optional[AdmissionSettings] = None,
                            sample_fn=None, engine=None) -> str:
    """Upstream's level string for this tick, classified against the budget.

    With a budget, the container's counters feed ``classify_pressure``; the
    ``elevated`` and ``critical`` thresholds then mean what upstream set them
    to mean, against the pod's budget instead of the sandbox's virtual
    memory. Without one, upstream's ``_memory_pressure_level`` decides.
    ``unknown`` becomes ``critical`` while ``fail_closed`` holds, with the
    cause logged; otherwise it falls back to upstream's reading.
    """
    engine = engine or _ENGINE
    fail_closed = DEFAULT_FAIL_CLOSED
    level = LEVEL_UNKNOWN
    cause = None
    budget_mib = None
    usage_mib = None
    source = None
    try:
        settings = settings or load_settings(engine=engine)
        fail_closed = settings.fail_closed
        budget_mib = settings.memory_budget_mib
        if budget_mib is None:
            level = engine.upstream_pressure_level()
            source = SOURCE_UPSTREAM
            if level == LEVEL_UNKNOWN:
                cause = "no budget is configured and upstream could not read /proc/meminfo"
        else:
            sample = (sample_fn or container_memory_sample)(budget_mib)
            if sample is None:
                cause = "no memory counter is readable (cgroup v1, cgroup v2, process tree)"
            else:
                level = engine.classify(sample.get(AVAILABLE_KEY), sample.get(TOTAL_KEY))
                usage_mib = int(sample.get(USAGE_KEY) or 0) // KIB_PER_MIB
                source = sample.get(SOURCE_KEY)
                if level == LEVEL_UNKNOWN:
                    cause = f"the sample from {source} did not classify"
            if level == LEVEL_UNKNOWN and not fail_closed:
                level = engine.upstream_pressure_level()
                source = SOURCE_UPSTREAM
    except Exception as exc:  # noqa: BLE001 - the tick decides anyway
        level = LEVEL_UNKNOWN
        cause = f"the pressure check raised {exc!r}"
    failed_closed = False
    if level == LEVEL_UNKNOWN and fail_closed:
        failed_closed = True
        level = LEVEL_CRITICAL
        logger.warning(
            "%s memory pressure is unknown (%s); fail_closed is on, so this tick spawns nothing",
            LOG_PREFIX, cause,
        )
    limited = level in LIMITING_LEVELS
    if limited and not failed_closed:
        logger.warning(
            "%s memory pressure is %s: %s MiB used of the %s MiB budget (%s)",
            LOG_PREFIX, level,
            "unknown" if usage_mib is None else f"{usage_mib:,}",
            "no" if budget_mib is None else f"{budget_mib:,}",
            source,
        )
    _record(
        result, **{
            RECORD_LEVEL: level, RECORD_LIMITED: limited, RECORD_CAUSE: cause,
            RECORD_FAIL_CLOSED: failed_closed, RECORD_BUDGET_MIB: budget_mib,
            RECORD_USAGE_MIB: usage_mib, RECORD_SOURCE: source,
        },
    )
    return level


def cap_spawn_budget(result: Any, spawn_budget: Optional[int]) -> Optional[int]:
    """Bound this tick's spawns by the budget's headroom, one worker per reserve.

    The pressure level is read once per tick, so an ``ok`` reading would let the
    ready loop spawn the whole slot budget at once, past the pod's budget before
    the next read. With a budget and a usage figure recorded on the result, the
    cap is ``(budget - usage) // WORKER_RESERVE_MIB``. A cap of zero is recorded
    as capacity-limiting so an overdue card is refused rather than left
    waiting; a smaller cap the loop can still spend is left to
    :func:`tick_was_limited`. Without a reading the budget is unchanged.
    """
    record = getattr(result, RESULT_ATTR, None)
    if not isinstance(record, Mapping):
        return spawn_budget
    budget_mib = record.get(RECORD_BUDGET_MIB)
    usage_mib = record.get(RECORD_USAGE_MIB)
    if not isinstance(budget_mib, int) or not isinstance(usage_mib, int):
        return spawn_budget
    cap = max(0, (budget_mib - usage_mib) // WORKER_RESERVE_MIB)
    if spawn_budget is not None and cap >= spawn_budget:
        return spawn_budget
    # A cap the ready loop can still spend is not a limit yet: tick_was_limited
    # sees the loop exhaust it. Only a cap of zero decides the tick here.
    fields = {RECORD_SPAWN_CAP: cap}
    if cap == 0:
        fields[RECORD_LIMITED] = True
    _record(result, **fields)
    logger.warning(
        "%s headroom of %s MiB bounds this tick to %s new worker(s) (slot budget %s)",
        LOG_PREFIX, f"{budget_mib - usage_mib:,}", cap,
        "uncapped" if spawn_budget is None else spawn_budget,
    )
    return cap


def tick_was_limited(result: Any, *, spawn_budget: Optional[int], spawned: int) -> bool:
    """Whether this tick held a ready card back for capacity.

    True when memory limited it (recorded by :func:`pressure_level_for_tick`),
    when its slot budget ran out, or when the sibling kanban_priority patch's
    per-class reservation held a row back.
    """
    record = getattr(result, RESULT_ATTR, None)
    if isinstance(record, Mapping) and record.get(RECORD_LIMITED):
        return True
    if spawn_budget is not None and spawned >= spawn_budget:
        return True
    return bool(getattr(result, RESERVED_FIELD, None))


def _excluded_ids(result: Any) -> set:
    excluded: set = set()
    for name in EXCLUDED_ID_FIELDS:
        excluded.update(getattr(result, name, ()) or ())
    for name in EXCLUDED_TUPLE_FIELDS:
        excluded.update(entry[0] for entry in (getattr(result, name, ()) or ()))
    return excluded


def _extend_auto_blocked(result: Any, task_ids: list) -> None:
    auto_blocked = getattr(result, AUTO_BLOCKED_FIELD, None)
    if isinstance(auto_blocked, list):
        auto_blocked.extend(task_ids)


def _own_reason(engine, conn, task_id: str, assignee: Any) -> Optional[str]:
    """Why the ready loop would skip this row for a reason of its own, or None.

    The loop records those reasons only for the rows it reached; a row behind
    an exhausted budget is asked the same questions here, so a card that is
    unrouted, aimed at no profile, or respawn-guarded is never told it waited
    for capacity.
    """
    if not assignee:
        return "unassigned"
    try:
        if engine.profile_exists(assignee) is False:
            return "nonspawnable"
    except Exception:  # noqa: BLE001 - an unanswerable question is not a reason
        pass
    try:
        guard = engine.respawn_guard_reason(conn, task_id)
    except Exception:  # noqa: BLE001
        guard = None
    return f"respawn_guarded:{guard}" if guard is not None else None


def refuse_overdue_ready(conn, result: Any, *, limited_by_capacity: bool,
                         settings: Optional[AdmissionSettings] = None, dry_run: bool = False,
                         now: Optional[float] = None, engine=None) -> list:
    """Block, with kind ``capacity``, the ready cards this tick left waiting past the bound.

    Only when ``limited_by_capacity`` holds: the tick was memory-limited or its
    slot budget was exhausted. A card the tick skipped for a reason of its own
    (``EXCLUDED_ID_FIELDS``, ``EXCLUDED_TUPLE_FIELDS``, or :func:`_own_reason`
    for a row the loop never reached) is never refused. The wait is measured
    from the card's last entry into the ready lane. Returns the ids blocked,
    which also join ``result.auto_blocked``.
    """
    if not limited_by_capacity or dry_run:
        return []
    engine = engine or _ENGINE
    refused: list = []
    try:
        settings = settings or load_settings(engine=engine)
        moment = int(time.time() if now is None else now)
        bound = settings.queue_bound_seconds
        excluded = _excluded_ids(result)
        rows = conn.execute(_OVERDUE_SQL, READY_ENTRY_EVENT_KINDS).fetchall()
        for row in rows:
            task_id, assignee, waited = row[0], row[1], moment - int(row[2])
            if task_id in excluded or waited < bound:
                continue
            own_reason = _own_reason(engine, conn, task_id, assignee)
            if own_reason is not None:
                logger.debug("%s %s waits for %s, not capacity; left alone", LOG_PREFIX, task_id, own_reason)
                continue
            reason = capacity_reason(
                f"the card waited {waited:,} s for a worker slot against the {bound:,} s bound"
            )
            try:
                blocked = engine.block_task(conn, task_id, reason=reason, kind=CAPACITY_KIND)
            except Exception as exc:  # noqa: BLE001 - one card never stops the others
                logger.warning("%s refusing %s failed: %r", LOG_PREFIX, task_id, exc)
                continue
            if blocked:
                refused.append(task_id)
                logger.warning("%s refused %s: %s", LOG_PREFIX, task_id, reason)
    except Exception as exc:  # noqa: BLE001 - the tick carries on
        logger.warning("%s refusing overdue cards failed: %r", LOG_PREFIX, exc)
    _record(result, **{RECORD_REFUSED: list(refused)})
    _extend_auto_blocked(result, refused)
    return refused


def _descendant_identities(pid: int, *, proc_root: str) -> list:
    """``(pid, start time)`` of each descendant, so a reused pid is not mistaken for it."""
    return [
        (member, _stat_field(proc_root, member, STAT_STARTTIME_INDEX))
        for member in process_tree(pid, proc_root=proc_root)[1:]
    ]


def _group_members(pid: int, *, proc_root: str) -> list:
    """Every other process in ``pid``'s process group that exists now.

    The dispatcher starts a worker as a session leader, so its group id is
    its pid, and what the worker forks without a session of its own stays in
    the group. A terminal tool's subprocess gets a session of its own and is
    outside this pass; the pressure guard, which reads the whole container,
    still counts it.
    """
    members: list = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return members
    for entry in entries:
        if not entry.isdigit() or int(entry) == int(pid):
            continue
        if _stat_field(proc_root, int(entry), STAT_PGRP_INDEX) == str(int(pid)):
            members.append(int(entry))
    return members


def _stop_worker(engine, pid: int, claim_lock: str, signal_fn, *, proc_root: str) -> dict:
    """Upstream's kill ladder on the worker, then whatever of its tree outlived it.

    ``_terminate_reclaimed_worker`` sends SIGTERM, waits a short grace and
    sends SIGKILL if the worker stays; ``terminated`` in its result says
    whether the worker is gone. Then two sweeps, because a worker's
    subprocesses hold the memory as much as the worker does: the descendants
    snapshotted before the SIGTERM, each checked by start time so a reused pid
    is not mistaken for one, and the live members of the worker's process
    group, which catches a child forked into the group during the grace.
    """
    descendants = _descendant_identities(pid, proc_root=proc_root)
    termination = engine.terminate(int(pid), claim_lock, signal_fn)
    outcome = {
        OUTCOME_PID: int(pid),
        OUTCOME_TERMINATED: bool(termination.get(TERMINATION_TERMINATED_KEY)),
        OUTCOME_SIGKILL: bool(termination.get(TERMINATION_SIGKILL_KEY)),
        OUTCOME_ORPHANS: [],
        OUTCOME_GROUP: [],
        OUTCOME_TERMINATION: termination,
    }
    kill = engine.kill_fn(signal_fn)
    if kill is None:
        return outcome
    for member, started in descendants:
        if started is not None and _stat_field(proc_root, member, STAT_STARTTIME_INDEX) == started:
            if engine.sigkill(kill, member):
                outcome[OUTCOME_ORPHANS].append(member)
    for member in _group_members(pid, proc_root=proc_root):
        if member not in outcome[OUTCOME_ORPHANS] and engine.sigkill(kill, member):
            outcome[OUTCOME_GROUP].append(member)
    return outcome


def _worker_outlived_the_ladder(engine, pid: int, outcome: Mapping[str, Any]) -> bool:
    """Whether the worker is still there after a ladder that could not take it down.

    Upstream reports ``terminated`` from one probe right after the SIGKILL,
    which a large address space still being torn down fails, and reports
    nothing delivered when the worker died between the poll and the SIGKILL.
    Neither is a survivor: a delivered SIGKILL is a death sentence, and a gone
    pid is gone. Only a worker the ladder could not signal, and that is still
    alive when asked again, keeps its card.
    """
    termination = outcome.get(OUTCOME_TERMINATION) or {}
    if not engine.survived(termination):
        return False
    if termination.get(TERMINATION_SIGKILL_KEY):
        return False
    return bool(engine.pid_alive(pid))


def enforce_worker_rss_caps(conn, *, signal_fn=None, settings: Optional[AdmissionSettings] = None,
                            rss_fn=None, engine=None, proc_root: str = PROC_ROOT) -> list:
    """Stop each host-local worker whose process tree is over the cap; block its card.

    Mirrors ``enforce_max_runtime``: host-local claims only, kill with grace,
    then the card leaves ``running``, here through ``block_task`` with kind
    ``capacity`` and the figures in the reason. A worker the ladder could not
    signal keeps its card, so a card is never blocked under a live worker the
    dispatcher cannot stop; the next tick measures it again. A worker with a
    SIGKILL delivered is treated as gone, whatever the first probe said.
    ``signal_fn`` and ``rss_fn`` are test hooks. Returns the ids blocked.
    """
    engine = engine or _ENGINE
    stopped: list = []
    try:
        settings = settings or load_settings(engine=engine)
        cap_mib = settings.worker_rss_cap_mib
        cap_kib = cap_mib * KIB_PER_MIB
        host_prefix = engine.host_prefix()
        rows = conn.execute(_RUNNING_SQL).fetchall()
        for row in rows:
            task_id, worker_pid, lock, run_id = row[0], row[1], row[2] or "", row[3]
            if not str(lock).startswith(host_prefix):
                continue
            pid = _positive_int(worker_pid)
            if pid is None:
                continue
            rss_kib = rss_fn(pid) if rss_fn is not None else process_tree_rss_kib(pid, proc_root=proc_root)
            if rss_kib is None or rss_kib <= cap_kib:
                continue
            outcome = _stop_worker(engine, pid, lock, signal_fn, proc_root=proc_root)
            if _worker_outlived_the_ladder(engine, pid, outcome):
                logger.warning(
                    "%s worker %s of %s is over the cap and survived termination (%s); "
                    "the card stays running and the next tick tries again",
                    LOG_PREFIX, pid, task_id, outcome,
                )
                continue
            reason = capacity_reason(
                f"the worker used {rss_kib // KIB_PER_MIB:,} MiB against the {cap_mib:,} MiB cap"
            )
            try:
                blocked = engine.block_task(
                    conn, task_id, reason=reason, kind=CAPACITY_KIND, expected_run_id=run_id,
                )
            except Exception as exc:  # noqa: BLE001 - one card never stops the others
                logger.warning("%s blocking %s after stopping worker %s failed: %r", LOG_PREFIX, task_id, pid, exc)
                continue
            if blocked:
                stopped.append(task_id)
                logger.warning("%s stopped worker %s of %s (%s): %s", LOG_PREFIX, pid, task_id, outcome, reason)
            else:
                logger.warning(
                    "%s stopped worker %s of %s (%s), but the card had already left running",
                    LOG_PREFIX, pid, task_id, outcome,
                )
    except Exception as exc:  # noqa: BLE001 - the tick carries on
        logger.warning("%s enforcing the worker RSS cap failed: %r", LOG_PREFIX, exc)
    return stopped
