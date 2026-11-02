"""An adaptive ceiling on ``kanban.max_in_progress``: step down on pressure, climb back slowly.

Installed into the image at ``/opt/hermes/hermes_cli/kanban_aimd.py``. Wired into
the dispatcher (``hermes_cli/kanban_db_dispatch.py``, the head of
``_tick_spawn_budget``) by edit 11 of ``deploy/docker/patches/apply_kanban_scheduling.py``,
and into the worker's terminal result (``tools/terminal_tool_result.py``,
``finalize_foreground_result``) by ``deploy/docker/patches/apply_kanban_aimd.py``.

Why
---
The dispatcher runs every card under one host-wide cap, ``kanban.max_in_progress``
(the operator pins 6). The cap is static: when the credential proxy answers a
worker ``429`` or ``503``, when a worker's sandbox command dies as a mux client
whose ssh master went away (exit 255, no cwd marker), or when the sandbox shell
is close to its memory limit, the dispatcher keeps filling every slot, and each
new worker adds to the pressure that is failing the ones already running.
Upstream's own guard reads the gateway container's memory, which is not where
these three signals show.

What
----
:class:`AimdConcurrencyLimiter` keeps an effective cap ``C_eff`` under the
configured ceiling ``C_max``, floor :data:`FLOOR`. A pressure signal sets
``C_eff = 1`` and arms a cooldown (:data:`DEFAULT_COOLDOWN_SECONDS`, overridable
through :data:`COOLDOWN_ENV`). A tick that arrives after the cooldown has lapsed
with no signal raises ``C_eff`` by one and re-arms the cooldown, until ``C_max``.
A signal during the cooldown restarts it. The limiter takes an injectable clock
so the unit tests drive it with a fake one.

Three sources feed it, read once per dispatch tick by
:func:`effective_max_in_progress`:

1. **Worker signals** (:func:`record_worker_pressure`), ``task_events`` rows of
   kind :data:`PRESSURE_KIND` on the worker's own card: an ssh result with exit
   255 and no cwd marker, or a command that exited with the credential proxy
   shim's busy status (:data:`BROKER_BUSY_EXIT`) and printed its busy line
   (:data:`BUSY_LINE_PREFIX`; ``agents/platform/scripts/credential_proxy_client.py``
   does both on a 429 or 503 its own retry did not get past). The status and
   the line together are the signal: text alone would let any file or log a
   worker reads steer the dispatcher. The dispatcher already reads this table,
   and it survives a gateway restart, which an in-memory channel would not.
   Only a worker (:data:`WORKER_TASK_ENV` set) records; the front door's own
   turns do not. One card steps the cap down at most once per cooldown: its
   further signals while the cooldown is armed are read and dropped, so a card
   that keeps hitting a busy broker cannot hold the cap at the floor alone.
2. **The sandbox shell's memory** (:func:`probe_shell_memory`), only while a
   worker is running (an idle board has nothing to hold back, and the probe
   would otherwise keep the shared ssh master alive on its own): one
   ``cat memory.current memory.max memory.stat`` over the ssh ControlMaster
   socket Hermes already holds for the shell, at the path
   ``tools/environments/ssh.py`` derives. Skipped when no socket exists, a no-op
   when ``memory.max`` is ``max`` or missing (no limit, or cgroup v1). What
   counts is what reclaim cannot free: ``memory.current`` less the file cache
   and reclaimable slab from ``memory.stat`` (the same accounting
   ``apply_cgroup_memory_sample.py`` uses for the gateway container), since a
   shell that has cloned a few repositories sits near its limit in page cache
   while healthy. Pressure above :data:`MEMORY_PRESSURE_RATIO` of the limit,
   and pressure when ssh itself exits 255: the sandbox did not answer. (A stale
   socket from a master that died uncleanly is not that case: with
   ``ControlMaster=no`` ssh falls back to a direct connection, so a dead master
   costs the probe a handshake, not the board a step-down.)
3. Upstream's gateway memory guard stays where it is, after this one, in
   ``_tick_spawn_budget``.

State is one row of :data:`STATE_TABLE` in the board's ``kanban.db``: the
configured and effective caps, the cooldown deadline, the ``task_events``
cursor the next tick reads from, and the cards that have already stepped the
cap down in the current cooldown. It is written when one of those changes, not
every tick. It survives a restart, and the session KV server reads it for the
board snapshot (``configured_max_in_progress`` and ``effective_max_in_progress``).
The first tick on a board starts the cursor at the newest event, so pressure
rows from before this build never pin a cap.

Nothing here may break a dispatch tick or a terminal result: every entry point
fails open, to the configured cap on the dispatcher side and to no record on the
worker side, and logs why.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

#: The clock every tick reads. Wall-clock, because the cooldown deadline is
#: persisted across restarts. Module-level so the build gate can pin it.
CLOCK: Callable[[], float] = time.time

#: The lowest the effective cap goes. A floor of 0 would stop dispatch outright,
#: and a tick that spawns nothing can never observe recovery.
FLOOR = 1

#: How long after a pressure signal the cap stays down, and how long between
#: recovery steps. The issue pins 60 seconds; the variable overrides it.
COOLDOWN_ENV = "KANBAN_AIMD_COOLDOWN_SECONDS"
DEFAULT_COOLDOWN_SECONDS = 60

#: The ``task_events`` kind a worker writes for a pressure signal.
PRESSURE_KIND = "pressure"

#: The limiter's row in ``kanban.db``. ``agents/platform/scripts/board_snapshot.py``
#: reads the same table by name; ``test_kanban_aimd`` pins the two spellings.
STATE_TABLE = "kube_aimd_state"
STATE_ROW_ID = 1
STATE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {STATE_TABLE} ("
    f" id INTEGER PRIMARY KEY CHECK (id = {STATE_ROW_ID}),"
    " configured INTEGER NOT NULL,"
    " effective INTEGER NOT NULL,"
    " cooldown_until REAL,"
    " last_event_id INTEGER NOT NULL,"
    " signalled TEXT NOT NULL DEFAULT '[]',"
    " updated_at REAL NOT NULL)"
)
#: A card's status while a worker runs it; the probe runs only while one does.
STATUS_RUNNING = "running"

#: The share of the sandbox shell's memory limit above which the shell counts
#: as under pressure.
MEMORY_PRESSURE_RATIO = 0.75
PERCENT = 100

#: The shell's cgroup v2 files, read in one ``cat`` so the probe is one round
#: trip; ``memory.max`` reads this when the container has no limit.
CGROUP_MEMORY_CURRENT = "/sys/fs/cgroup/memory.current"
CGROUP_MEMORY_MAX = "/sys/fs/cgroup/memory.max"
CGROUP_MEMORY_STAT = "/sys/fs/cgroup/memory.stat"
CGROUP_UNLIMITED = "max"
PROBE_COMMAND = f"cat {CGROUP_MEMORY_CURRENT} {CGROUP_MEMORY_MAX} {CGROUP_MEMORY_STAT}"
#: The ``memory.stat`` keys reclaim can free before an OOM kill: the file cache
#: and reclaimable slab. ``apply_cgroup_memory_sample.py`` subtracts the same.
RECLAIMABLE_KEYS = ("active_file", "inactive_file", "slab_reclaimable")

#: ssh's exit status when the connection failed or the mux master went away,
#: as opposed to the remote command's own status.
SSH_MUX_EXIT = 255
SSH_DEFAULT_PORT = 22
#: Bounds the probe: ssh's ConnectTimeout and the subprocess wait. Below the
#: dispatcher's own tick so a hung shell costs one tick, not a queue of them.
PROBE_TIMEOUT_SECONDS = 10
#: Where ``tools/environments/ssh.py`` keeps its ControlMaster sockets:
#: ``<tempdir>/hermes-ssh/<sha256(user@host:port)[:16]>.sock``.
SSH_CONTROL_DIR_NAME = "hermes-ssh"
SSH_SOCKET_ID_CHARS = 16
SSH_SOCKET_SUFFIX = ".sock"
SSH_BACKEND = "ssh"

#: Set by the dispatcher in every worker it spawns, to the worker's own card.
WORKER_TASK_ENV = "HERMES_KANBAN_TASK"
WORKER_RUN_ENV = "HERMES_KANBAN_RUN_ID"

#: The line the credential proxy shim prints on a 429 or 503, followed by the
#: status and a closing parenthesis, and the status it then exits with
#: (``EX_TEMPFAIL``). The shim spells both; ``test_kanban_aimd`` pins the two
#: files against each other.
BUSY_LINE_PREFIX = "credential proxy busy (HTTP "
BROKER_BUSY_EXIT = 75

#: The ``reason`` a pressure event or a probe verdict carries.
REASON_BROKER_BUSY = "broker_busy"
REASON_SSH_EXIT = "ssh_exit_255"
REASON_SHELL_MEMORY = "shell_memory"
REASON_PROBE_EXIT = "probe_exit_255"
SOURCE_WORKER = "worker"


def cooldown_seconds() -> float:
    """The cooldown in force: :data:`COOLDOWN_ENV`, or the default when unset or unreadable."""
    raw = os.environ.get(COOLDOWN_ENV)
    if raw is None or not raw.strip():
        return float(DEFAULT_COOLDOWN_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        logger.warning("kanban aimd: %s=%r is not a number; using %ss", COOLDOWN_ENV, raw, DEFAULT_COOLDOWN_SECONDS)
        return float(DEFAULT_COOLDOWN_SECONDS)
    if value <= 0:
        logger.warning("kanban aimd: %s=%r is not positive; using %ss", COOLDOWN_ENV, raw, DEFAULT_COOLDOWN_SECONDS)
        return float(DEFAULT_COOLDOWN_SECONDS)
    return value


# ---------------------------------------------------------------------------
# The limiter
# ---------------------------------------------------------------------------


class AimdConcurrencyLimiter:
    """Additive-increase, hard-step-down cap under a configured ceiling.

    ``c_max`` is the ceiling (``kanban.max_in_progress``), clamped to at least
    :data:`FLOOR`. ``clock`` returns the current time in seconds; ``probe`` is
    an optional callable the tick asks for a pressure verdict when none is
    passed in (truthy means pressure). ``effective`` and ``cooldown_until``
    restore a persisted state; the effective cap is clamped to the ceiling, so
    a lowered ``c_max`` takes effect at once.
    """

    def __init__(
        self,
        c_max: int,
        *,
        clock: Callable[[], float] = CLOCK,
        probe: Optional[Callable[[], Any]] = None,
        cooldown: Optional[float] = None,
        effective: Optional[int] = None,
        cooldown_until: Optional[float] = None,
    ) -> None:
        self.c_max = max(FLOOR, int(c_max))
        self.clock = clock
        self.probe = probe
        self.cooldown = float(cooldown) if cooldown is not None else cooldown_seconds()
        self.effective = self.c_max if effective is None else max(FLOOR, min(self.c_max, int(effective)))
        self.cooldown_until = None if cooldown_until is None else float(cooldown_until)

    def _now(self, now: Optional[float]) -> float:
        return float(self.clock() if now is None else now)

    def signal(self, now: Optional[float] = None) -> int:
        """A pressure signal: the cap drops to the floor and the cooldown (re)starts."""
        now = self._now(now)
        self.effective = FLOOR
        self.cooldown_until = now + self.cooldown
        return self.effective

    def tick(self, pressure: Optional[bool] = None, now: Optional[float] = None) -> int:
        """One dispatcher tick: step down on pressure, else step up once the cooldown lapsed.

        One step per tick however long the cooldown has been over, so a
        dispatcher that was paused comes back one slot at a time rather than
        all at once; the deadline is re-armed from now.
        """
        now = self._now(now)
        if pressure is None and self.probe is not None:
            pressure = bool(self.probe())
        if pressure:
            return self.signal(now)
        if self.cooldown_until is not None and now >= self.cooldown_until:
            self.effective = min(self.c_max, self.effective + 1)
            self.cooldown_until = now + self.cooldown if self.effective < self.c_max else None
        elif self.cooldown_until is None and self.effective < self.c_max:
            # Below a ceiling that was raised while the cap sat at the old
            # one: start the climb rather than staying there for good.
            self.cooldown_until = now + self.cooldown
        return self.effective


# ---------------------------------------------------------------------------
# Persistence in kanban.db
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _write(conn: sqlite3.Connection):
    """Hermes's ``write_txn`` on the board; a plain IMMEDIATE transaction outside the image."""
    try:
        from hermes_cli.kanban_db_connect import write_txn
    except ImportError:  # unit tests import the patch modules flat
        write_txn = None
    if write_txn is not None:
        with write_txn(conn):
            yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def load_state(conn: sqlite3.Connection) -> Optional[dict]:
    """The persisted row, or None before the first tick (or on an older board)."""
    try:
        row = conn.execute(
            f"SELECT configured, effective, cooldown_until, last_event_id, signalled, updated_at "
            f"FROM {STATE_TABLE} WHERE id = ?",
            (STATE_ROW_ID,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        signalled = [str(card) for card in json.loads(row[4] or "[]")]
    except (TypeError, ValueError):
        signalled = []
    return {
        "configured": int(row[0]),
        "effective": int(row[1]),
        "cooldown_until": None if row[2] is None else float(row[2]),
        "last_event_id": int(row[3]),
        "signalled": signalled,
        "updated_at": float(row[5]),
    }


def save_state(
    conn: sqlite3.Connection,
    *,
    configured: int,
    effective: int,
    cooldown_until: Optional[float],
    last_event_id: int,
    now: float,
    signalled: Sequence[str] = (),
) -> None:
    """Write the row; ``updated_at`` is when it last changed, not the last tick."""
    with _write(conn):
        conn.execute(STATE_DDL)
        conn.execute(
            f"INSERT INTO {STATE_TABLE} "
            "(id, configured, effective, cooldown_until, last_event_id, signalled, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "configured = excluded.configured, effective = excluded.effective, "
            "cooldown_until = excluded.cooldown_until, last_event_id = excluded.last_event_id, "
            "signalled = excluded.signalled, updated_at = excluded.updated_at",
            (
                STATE_ROW_ID, int(configured), int(effective), cooldown_until, int(last_event_id),
                json.dumps(sorted(signalled)), float(now),
            ),
        )


def latest_event_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0])


def pending_pressure(conn: sqlite3.Connection, after_id: int, up_to_id: int) -> list[str]:
    """The cards with a pressure event in ``(after_id, up_to_id]``, each once.

    The upper bound is the newest id the tick read first, so a row a worker
    writes between the two reads waits for the next tick rather than being
    counted by both.
    """
    rows = conn.execute(
        "SELECT DISTINCT task_id FROM task_events WHERE kind = ? AND id > ? AND id <= ? ORDER BY task_id",
        (PRESSURE_KIND, int(after_id), int(up_to_id)),
    ).fetchall()
    return [str(row[0]) for row in rows]


def running_workers(conn: sqlite3.Connection) -> int:
    """How many cards a worker is running now; the probe runs only while one is."""
    return int(
        conn.execute("SELECT COUNT(*) FROM tasks WHERE status = ?", (STATUS_RUNNING,)).fetchone()[0]
    )


# ---------------------------------------------------------------------------
# The sandbox shell's memory, over the shared ssh master
# ---------------------------------------------------------------------------


def ssh_target() -> Optional[tuple[str, str, int, str]]:
    """``(user, host, port, key_path)`` of the terminal's ssh backend, or None when it is not ssh."""
    try:
        from hermes_cli.config import load_config_readonly

        terminal = (load_config_readonly() or {}).get("terminal") or {}
    except Exception:  # noqa: BLE001 — no config is no probe
        return None
    if not isinstance(terminal, dict) or terminal.get("backend") != SSH_BACKEND:
        return None
    host, user = terminal.get("ssh_host"), terminal.get("ssh_user")
    if not host or not user:
        return None
    try:
        port = int(terminal.get("ssh_port") or SSH_DEFAULT_PORT)
    except (TypeError, ValueError):
        port = SSH_DEFAULT_PORT
    return str(user), str(host), port, str(terminal.get("ssh_key") or "")


def control_socket(user: str, host: str, port: int) -> Path:
    """The ControlMaster socket ``SSHEnvironment`` derives for this target."""
    socket_id = hashlib.sha256(f"{user}@{host}:{port}".encode()).hexdigest()[:SSH_SOCKET_ID_CHARS]
    return Path(tempfile.gettempdir()) / SSH_CONTROL_DIR_NAME / f"{socket_id}{SSH_SOCKET_SUFFIX}"


def classify_memory(text: str) -> Optional[str]:
    """The probe's verdict from ``memory.current``, ``memory.max`` and ``memory.stat``: a reason, or None.

    What is measured is ``memory.current`` less the :data:`RECLAIMABLE_KEYS` of
    ``memory.stat``: page cache and reclaimable slab are freed before an OOM
    kill, and a shell that has read a few repositories carries most of its
    limit as cache while healthy. None when the limit is ``max`` or absent,
    when either figure is not a number, or when what remains is at or under
    :data:`MEMORY_PRESSURE_RATIO` of the limit. Missing ``memory.stat`` lines
    count as nothing reclaimable, the conservative reading.
    """
    lines = (text or "").split("\n")
    head = " ".join(lines[:2]).split()
    if len(head) < 2 or head[1] == CGROUP_UNLIMITED:
        return None
    try:
        current, limit = int(head[0]), int(head[1])
    except ValueError:
        return None
    if limit <= 0:
        return None
    reclaimable = 0
    for line in lines[2:]:
        fields = line.split()
        if len(fields) == 2 and fields[0] in RECLAIMABLE_KEYS and fields[1].isdigit():
            reclaimable += int(fields[1])
    used = max(0, current - reclaimable)
    if used > limit * MEMORY_PRESSURE_RATIO:
        return f"{REASON_SHELL_MEMORY} {used * PERCENT // limit}%"
    return None


def probe_shell_memory(
    target: Optional[tuple[str, str, int, str]] = None,
    runner: Callable[..., Any] = subprocess.run,
) -> Optional[str]:
    """Sample the sandbox shell over the existing ssh master; a reason when it is under pressure.

    None, with no connection made, when the terminal is not ssh or no master
    socket exists yet. ``ControlMaster=no`` keeps the probe from opening a
    master of its own; it also means a stale socket (a master that died
    uncleanly) is bypassed with a direct connection rather than failed, so
    only a sandbox that does not answer gives ssh's own 255, and that is
    pressure. The remote ``cat`` failing (no cgroup v2 files) is a no-op. A
    probe that outruns its timeout is logged and counts for nothing: the next
    tick asks again.
    """
    target = ssh_target() if target is None else target
    if target is None:
        return None
    user, host, port, key_path = target
    socket = control_socket(user, host, port)
    if not socket.exists():
        return None
    cmd = [
        "ssh",
        "-o", f"ControlPath={socket}",
        "-o", "ControlMaster=no",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={PROBE_TIMEOUT_SECONDS}",
    ]
    if port != SSH_DEFAULT_PORT:
        cmd.extend(["-p", str(port)])
    if key_path:
        cmd.extend(["-i", key_path])
    cmd.extend([f"{user}@{host}", PROBE_COMMAND])
    try:
        proc = runner(
            cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        logger.warning("kanban aimd: the shell memory probe timed out after %ss", PROBE_TIMEOUT_SECONDS)
        return None
    except OSError as exc:
        logger.warning("kanban aimd: the shell memory probe could not run: %r", exc)
        return None
    if proc.returncode == SSH_MUX_EXIT:
        return REASON_PROBE_EXIT
    if proc.returncode != 0:
        logger.debug("kanban aimd: the shell has no readable cgroup memory files (exit %s)", proc.returncode)
        return None
    return classify_memory(proc.stdout)


# ---------------------------------------------------------------------------
# The dispatcher side: one call per tick
# ---------------------------------------------------------------------------


def effective_max_in_progress(
    conn: sqlite3.Connection,
    configured: Optional[int],
    board: Optional[str] = None,
    *,
    now: Optional[float] = None,
    probe: Optional[Callable[[], Optional[str]]] = None,
) -> Optional[int]:
    """The cap this tick dispatches under, given the configured one.

    Reads the worker signals since the last tick and, while a worker is
    running, the shell probe; steps the persisted limiter; writes the state
    back when it changed; and logs each step. A card counts once per armed
    cooldown: its later signals are read and dropped. ``None`` stays ``None``
    (an uncapped dispatcher has no ceiling to lower). ``board`` is the
    dispatcher's own parameter and is only named in the log. Never raises: on
    any error the configured cap is returned unchanged.
    """
    if configured is None:
        return None
    try:
        c_max = int(configured)
    except (TypeError, ValueError):
        return configured
    if c_max < FLOOR:
        return configured
    try:
        now = CLOCK() if now is None else float(now)
        latest = latest_event_id(conn)
        state = load_state(conn)
        reasons: list[str] = []
        signalled: list[str] = []
        if state is None:
            limiter = AimdConcurrencyLimiter(c_max)
        else:
            limiter = AimdConcurrencyLimiter(
                c_max, effective=state["effective"], cooldown_until=state["cooldown_until"],
            )
            cursor = int(state["last_event_id"])
            if cursor > latest:
                # A rebuilt table hands ids out again from the start, so
                # nothing says which rows this cursor has seen. Read them
                # all: one spurious step-down is the conservative error.
                logger.info("kanban aimd: the event cursor (%d) is past the newest event (%d); rereading", cursor, latest)
                cursor = 0
            armed = limiter.cooldown_until is not None and now < limiter.cooldown_until
            signalled = list(state["signalled"]) if armed else []
            cards = pending_pressure(conn, cursor, latest)
            fresh = [card for card in cards if card not in signalled]
            if len(fresh) < len(cards):
                logger.debug(
                    "kanban aimd: %d card(s) signalled again inside the cooldown; not counted",
                    len(cards) - len(fresh),
                )
            if fresh:
                reasons.append(f"{len(fresh)} worker signal(s)")
                signalled.extend(fresh)
        verdict = None
        if running_workers(conn):
            verdict = (probe_shell_memory if probe is None else probe)()
        if verdict:
            reasons.append(str(verdict))
        before = limiter.effective
        effective = limiter.tick(pressure=bool(reasons), now=now)
        if limiter.cooldown_until is None:
            signalled = []  # back at the ceiling: every card may signal again
        changed = state is None or (
            state["configured"], state["effective"], state["cooldown_until"],
            state["last_event_id"], sorted(state["signalled"]),
        ) != (c_max, effective, limiter.cooldown_until, latest, sorted(signalled))
        if changed:
            save_state(
                conn, configured=c_max, effective=effective, cooldown_until=limiter.cooldown_until,
                last_event_id=latest, now=now, signalled=signalled,
            )
        if reasons:
            logger.warning(
                "kanban aimd: pressure (%s); effective max_in_progress %d -> %d of %d for %ss%s",
                ", ".join(reasons), before, effective, c_max, int(limiter.cooldown),
                f" on board {board}" if board else "",
            )
        elif effective != before:
            logger.info(
                "kanban aimd: clean interval; effective max_in_progress %d -> %d of %d%s",
                before, effective, c_max, f" on board {board}" if board else "",
            )
        return effective
    except Exception as exc:  # noqa: BLE001 — never break the dispatch tick
        logger.warning("kanban aimd: falling back to the configured cap %r: %r", configured, exc)
        return configured


# ---------------------------------------------------------------------------
# The worker side: a pressure event on the worker's own card
# ---------------------------------------------------------------------------


def worker_pressure_reason(env_type: str, returncode: int, output: str, cwd_observed: bool) -> Optional[str]:
    """Why a finished command is a pressure signal, or None.

    An ssh result with exit 255 and no cwd marker is the mux client whose
    master went away, or a connection that never opened (the shape
    ``ssh_shared_master`` hints on). A command that exited with the shim's
    busy status and whose output has the busy line at the start of a line is
    a 429 or 503 the shim could not retry past. The line alone is not enough:
    a log, a file or a README the worker reads could carry it.
    """
    if env_type == SSH_BACKEND and returncode == SSH_MUX_EXIT and not cwd_observed:
        return REASON_SSH_EXIT
    if returncode == BROKER_BUSY_EXIT and any(
        line.startswith(BUSY_LINE_PREFIX) for line in (output or "").splitlines()
    ):
        return REASON_BROKER_BUSY
    return None


def _default_connect() -> sqlite3.Connection:
    """The worker's own board, as the dispatcher pinned it in ``HERMES_KANBAN_DB``."""
    from hermes_cli.kanban_db_connect import connect

    return connect()


def record_worker_pressure(
    env_type: str,
    returncode: int,
    output: str,
    cwd_observed: bool = False,
    *,
    task_id: Optional[str] = None,
    connect: Optional[Callable[[], sqlite3.Connection]] = None,
) -> Optional[str]:
    """Append a ``pressure`` event to the worker's card when the result is a signal.

    Returns the reason recorded, or None when nothing was: not a worker
    (no :data:`WORKER_TASK_ENV`), or not a signal. The caller wraps this in
    the terminal tool's fail-open block, so an unwritable board costs a debug
    line and never the command's result. A board opened here is closed here;
    one a caller's ``connect`` hands over is the caller's to close.
    """
    task_id = task_id or os.environ.get(WORKER_TASK_ENV)
    if not task_id:
        return None
    reason = worker_pressure_reason(env_type, returncode, output, cwd_observed)
    if reason is None:
        return None
    run_id: Optional[int] = None
    raw_run = os.environ.get(WORKER_RUN_ENV)
    if raw_run and raw_run.isdigit():
        run_id = int(raw_run)
    payload = {"reason": reason, "source": SOURCE_WORKER, "exit_code": int(returncode)}
    owned = connect is None
    conn = _default_connect() if owned else connect()
    try:
        with _write(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (task_id, run_id, PRESSURE_KIND, json.dumps(payload), int(CLOCK())),
            )
    finally:
        if owned:
            conn.close()
    logger.info("kanban aimd: card %s recorded pressure (%s)", task_id, reason)
    return reason
