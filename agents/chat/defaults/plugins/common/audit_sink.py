"""The file an audit record goes to: the profile's own ``logs/audit.jsonl``.

The ``tool_call_audit`` plugin and the ``chat_message_audit`` hook build their
records with ``audit_schema.envelope`` and hand them to :func:`emit`, which
appends one JSON object per line to a file under the profile's ``logs/``
directory. The fluent-bit sidecar in the gateway pod
tails that file with its JSON parser (``buildFluentBitConfigMap`` in the
operator), so the record's keys reach Cloud Logging as ``jsonPayload`` fields
without a regex over anything Hermes wrote. The emitters used to log the
record through Hermes' logger and the sidecar lifted the object back out of
the formatted line by its prefix — a prefix that was Hermes' to change, and
that any logger writing untrusted text could have imitated. Closing that route
leaves the file itself as the boundary: any process under the agent's uid with a
path to the profile's ``logs/`` can append a line the sidecar ships as a record,
which is the volume's boundary, not this sink's (the operator's
``buildFluentBitConfigMap`` says the same).

Which profile's file, and the ``agent_profile`` stamped on the record: the one the record is emitted under.
``hermes_constants.get_hermes_home()`` is how the running Hermes names it —
the context-local override the gateway sets around a turn it serves for
another profile, then ``HERMES_HOME``, which a kanban worker is launched with
pointing at its own profile — so the file follows the record to
``/opt/data/logs/`` for the front door and ``/opt/data/profiles/<name>/logs/``
for a named profile, the same ``logs/`` Hermes routes its own ``agent.log``
to. Outside a Hermes process (the unit tests) ``HERMES_HOME`` alone decides.
A missing ``logs/`` is made the way Hermes makes its own, through
``mkdir_under_hermes_home``, which refuses a named profile that is missing or
tombstoned, so an emitter cannot bring a pruned profile's directory back.

Redaction is two layers. The emitters run ``AuditRedactor`` over the fields
they know to be dangerous — arguments, results, message text, the principal.
The serialised line then passes through Hermes' own ``redact_sensitive_text``
when the process is Hermes, the engine ``RedactingFormatter`` applied to the
records while they went through ``agent.log`` and whose prefix list is wider
than ``AuditRedactor``'s; it runs forced, so a profile's
``security.redact_secrets: false`` does not reopen a trail that leaves the
pod. Outside Hermes the line is written as it is.

Where the write happens. Two of the callers run on the gateway's event loop
(``pre_gateway_dispatch`` is called synchronously there, and the chat hook
is awaited there), so :func:`emit` hands the write to a single writer thread
when it finds a running loop on the calling thread, in submission order, and
writes inline from any other thread. The profile is resolved on the calling
thread either way, because the per-turn override is context-local and the
writer thread has no context of its own.

Every write opens the file itself, append-only, and closes it, under an
exclusive ``flock`` on ``audit.jsonl.lock`` beside it. The gateway and its
worker processes share a profile's file and no handle, and the lock is what
orders them through a rotation and the write that follows it: no two writers
rotate at once, and a live file one of them has just created is never moved
by another. The lock is taken without blocking and retried for
``LOCK_WAIT_SECONDS``: the plugin's ``pre_tool_call`` runs inside Hermes'
fail-closed hook timeout, and a tool call must not hang on a stuck writer. A
write that fails part-way is cut back to where it started, so a fragment
cannot fuse with the next record. Rotation is the emitters' own: at the cap
the file is renamed to ``.1`` (``.2``, ``.3``), the numbers Hermes uses for
its ``agent.log``, so the volume holds a bounded trail. The sidecar keeps a
rotated inode open for its ``Rotate_Wait`` (30 s) and opens the new live file
on its next refresh (5 s), so the one way a record goes unshipped is a
sidecar more than 30 s behind the file at the moment it rotates.

When the file cannot take a record — a full or read-only volume, a lock held
past the wait, a profile Hermes refuses to materialise, or the single writer
thread pinned by a write that blocks rather than returns with
``MAX_PENDING_WRITES`` records already behind it — the record is printed to
this process's stdout as the same one JSON line, and an ERROR
naming the file and the error, carrying neither the record nor its keys, goes
to Hermes' logger. What the two reach depends on the process. From the
gateway the stdout is the container log, which the GKE log agent ships to
Cloud Logging as ``jsonPayload`` under the agent container, so
``jsonPayload.audit_event:*`` and the Admin Console's field-form query still
find the record, and the ERROR lands in the front door's ``agent.log``, which
the sidecar tails. From a kanban worker neither reaches Cloud Logging: the
stdout is the worker's captured transcript, the board's ``logs/<task>.log`` on
the same volume, and the ERROR lands in the named profile's ``agent.log``,
which the sidecar's ``*.log`` input under the front door's ``logs/`` does not
reach. On a full volume the transcript write fails too, and the ERROR is the
only trace, if it can be written at all.
"""

import asyncio
import concurrent.futures
import fcntl
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

AUDIT_FILE_NAME = "audit.jsonl"
# Appended to the audit file's name for the lock file beside it: outside both
# globs the sidecar tails, `audit.jsonl` and `*.log`.
AUDIT_LOCK_FILE_SUFFIX = ".lock"
LOGS_DIR_NAME = "logs"
HERMES_HOME_ENV = "HERMES_HOME"
# What the agent image sets HERMES_HOME to, and the default every script in
# this tree falls back to when the variable is unset.
DEFAULT_HERMES_HOME = "/opt/data"
# The record field naming the profile whose audit file the record lands in, so a
# record the sidecar tails is self-describing. The Admin Console reads it as the
# agent name (telemetry.normalize_logging_row); without it a tailed record falls
# back to the fluent-bit container name.
AGENT_PROFILE_FIELD = "agent_profile"
# A named profile's home is <hermes-home>/profiles/<name>: the directory just
# under the home that marks one, and the name a record gets when its home is the
# home itself (the front door).
PROFILES_DIR_NAME = "profiles"
DEFAULT_PROFILE_NAME = "default"
# Owner read-write, group read. Every container the operator renders onto the
# volume runs as the same uid, so the sidecar reads the file as its owner; the
# group bit is for a reader a CR adds under a uid of its own, and no uid but
# the owner writes it.
AUDIT_FILE_MODE = 0o640
# Hermes' own defaults for agent.log (hermes_logging.setup_logging). The same
# cap also bounds what the sidecar re-reads from the head of the file after a
# restart, because its tail position lives in an emptyDir.
AUDIT_FILE_MAX_BYTES = 5 * 1024 * 1024
AUDIT_FILE_BACKUP_COUNT = 3
# How long a write waits for another writer's lock before the record takes the
# stdout path. A healthy writer holds the lock for microseconds; the bound is
# there for a stuck one, because pre_tool_call runs inside Hermes' hook timeout
# (plugins.hook_callback_timeout, 30 s by default) and blocks the tool call.
LOCK_WAIT_SECONDS = 1.5
LOCK_RETRY_INTERVAL_SECONDS = 0.05
WRITER_THREAD_NAME = "audit-sink"
# The depth at which emit() stops handing writes to the writer thread and prints
# to stdout instead. A healthy writer drains in microseconds, so the backlog is
# zero or one; a write that blocks rather than fails -- a stalled volume, an
# os.write that does not return -- pins the one writer thread, and without a cap
# every later event would queue in memory unbounded, reaching neither the file
# nor the stdout fallback _write takes only on an error. The cap bounds the
# queue to MAX_PENDING_WRITES records and keeps the trail on stdout.
MAX_PENDING_WRITES = 256
_OPEN_FLAGS = os.O_WRONLY | os.O_APPEND | os.O_CREAT
_ENCODING = "utf-8"

_writer: Optional[concurrent.futures.ThreadPoolExecutor] = None
_writer_lock = threading.Lock()
# Writes handed to the writer thread and not yet finished; guarded by _writer_lock.
_pending_writes = 0


def hermes_home() -> Path:
    """The home of the profile the current record belongs to."""
    try:
        from hermes_constants import get_hermes_home  # the running Hermes' own
    except ImportError:
        get_hermes_home = None
    if get_hermes_home is not None:
        try:
            return Path(get_hermes_home())
        except Exception:
            pass
    return Path(os.environ.get(HERMES_HOME_ENV) or DEFAULT_HERMES_HOME)


def audit_file_path(home: Optional[Path] = None) -> Path:
    """``<home>/logs/audit.jsonl``, for the current profile unless ``home`` is given."""
    return (home or hermes_home()) / LOGS_DIR_NAME / AUDIT_FILE_NAME


def profile_name(home: Path) -> str:
    """The profile ``home`` belongs to: ``<name>`` for a ``profiles/<name>`` home, else the default.

    A named profile's home is ``<hermes-home>/profiles/<name>``; the front door's
    home is the hermes home itself, whose records are the default profile's.
    """
    if home.parent.name == PROFILES_DIR_NAME:
        return home.name
    return DEFAULT_PROFILE_NAME


def lock_file_path(path: Path) -> Path:
    """The lock file beside the audit file at ``path``."""
    return path.with_name(path.name + AUDIT_LOCK_FILE_SUFFIX)


def serialize(record: Dict[str, Any]) -> str:
    """One line: the object with sorted keys, every newline inside a value escaped."""
    return json.dumps(record, default=str, sort_keys=True)


def redact(line: str) -> str:
    """Hermes' own redaction over the serialised line when the process is Hermes, else the line."""
    try:
        from agent.redact import redact_sensitive_text  # the running Hermes' own
    except ImportError:
        return line
    return redact_sensitive_text(line, force=True)


def emit(record: Dict[str, Any], logger: logging.Logger) -> Optional[concurrent.futures.Future]:
    """Write ``record`` to the audit file: inline, or on the writer thread when called on an event loop.

    Returns the pending write when it was handed to the writer thread, and None
    when it was done inline or taken straight to stdout because the writer thread
    had ``MAX_PENDING_WRITES`` writes already waiting. A write that fails does not
    reach the caller: the record is printed to this process's stdout as the same
    JSON line and the failure goes to ``logger`` as an ERROR naming the file and
    the error and nothing of the record (see the module docstring for what each
    reaches).

    Before serialisation the record is stamped with ``AGENT_PROFILE_FIELD``, the
    profile it is emitted under, so a tailed line names its own profile.
    """
    # Resolved here, on the caller's thread: the per-turn profile override is
    # context-local, and the writer thread has no context of its own. The home
    # names the profile, stamped onto the record so a tailed line is self-
    # describing (the console reads AGENT_PROFILE_FIELD as the agent name).
    home = hermes_home()
    record = {**record, AGENT_PROFILE_FIELD: profile_name(home)}
    line = redact(serialize(record))
    path = audit_file_path(home)
    if not _on_event_loop():
        _write(line, path, logger)
        return None
    global _pending_writes
    writer = _writer_thread()
    with _writer_lock:
        backlog = _pending_writes
        if backlog < MAX_PENDING_WRITES:
            _pending_writes += 1
    if backlog >= MAX_PENDING_WRITES:
        # The writer thread is not draining -- a write that blocks rather than
        # fails never reaches _write's stdout fallback. Take that path here, so
        # the record is kept and the in-memory queue stays bounded.
        _to_stdout(line, path, logger, f"the writer thread is backed up, {backlog} writes pending")
        return None
    future = writer.submit(_write, line, path, logger)
    future.add_done_callback(_writer_done)
    return future


def flush(timeout: Optional[float] = None) -> None:
    """Wait for every write handed to the writer thread so far."""
    with _writer_lock:
        writer = _writer
    if writer is not None:
        writer.submit(lambda: None).result(timeout)


def append_line(line: str, path: Optional[Path] = None) -> Path:
    """Append ``line`` and a newline to the audit file, creating or rotating it as needed.

    Returns the path written. Raises when the file cannot be written: ``OSError``
    from the volume, ``TimeoutError`` for a lock held past ``LOCK_WAIT_SECONDS``,
    or Hermes' ``FileNotFoundError`` for a named profile it refuses to
    materialise.
    """
    path = path or audit_file_path()
    data = (line + "\n").encode(_ENCODING)
    # The lock file is opened first, so a missing logs/ is made here. flock is
    # held by the open file description, so two threads of one process contend
    # on it exactly as two processes do, and closing the descriptor releases it.
    lock_path = lock_file_path(path)
    lock_fd = _open(lock_path)
    try:
        _acquire(lock_fd, lock_path)
        _rotate_if_full(path, len(data))
        fd = _open(path)
        try:
            _write_whole(fd, data)
        finally:
            os.close(fd)
    finally:
        os.close(lock_fd)
    return path


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _writer_thread() -> concurrent.futures.ThreadPoolExecutor:
    """The one writer thread, made on first use. One worker is what keeps the order."""
    global _writer
    with _writer_lock:
        if _writer is None:
            _writer = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=WRITER_THREAD_NAME
            )
        return _writer


def _writer_done(_future: object) -> None:
    """One write finished: drop it from the backlog count."""
    global _pending_writes
    with _writer_lock:
        if _pending_writes > 0:
            _pending_writes -= 1


def _write(line: str, path: Path, logger: logging.Logger) -> None:
    try:
        append_line(line, path)
    except Exception as exc:
        _to_stdout(line, path, logger, exc)


def _to_stdout(line: str, path: Path, logger: logging.Logger, failure: object) -> None:
    """Print the record to stdout as the fallback trail; ERROR if even that fails.

    ``failure`` is the write error, or the reason the write was not attempted at
    all -- the writer thread backed up. Neither the record nor its keys reach the
    ERROR, so the console's text-form query does not count it.
    """
    try:
        print(line, file=sys.stdout, flush=True)
    except Exception as stdout_failure:
        logger.error(
            "audit record not written to %s (%s) and not printed to stdout (%s); the record is lost",
            path, failure, stdout_failure,
        )
        return
    logger.error("audit record not written to %s (%s); printed to this process's stdout instead", path, failure)


def _open(path: Path) -> int:
    try:
        return os.open(path, _OPEN_FLAGS, AUDIT_FILE_MODE)
    except FileNotFoundError:
        # The first record of a profile can come before anything made its
        # logs/ directory; Hermes' own setup makes it later for agent.log.
        _make_directory(path.parent)
        return os.open(path, _OPEN_FLAGS, AUDIT_FILE_MODE)


def _make_directory(directory: Path) -> None:
    """Make ``directory`` the way Hermes would; plainly outside Hermes.

    ``mkdir_under_hermes_home`` refuses a named profile home that is missing or
    tombstoned, so a record emitted after a profile was pruned cannot bring its
    directory back; its refusal propagates and the record takes the stdout path.
    """
    try:
        from hermes_constants import mkdir_under_hermes_home
    except ImportError:
        directory.mkdir(parents=True, exist_ok=True)
        return
    mkdir_under_hermes_home(directory)


def _acquire(lock_fd: int, lock_path: Path) -> None:
    """Take the lock without blocking, retrying for LOCK_WAIT_SECONDS; then give up."""
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"another writer has held {lock_path} for {LOCK_WAIT_SECONDS}s")
            time.sleep(LOCK_RETRY_INTERVAL_SECONDS)


def _write_whole(fd: int, data: bytes) -> None:
    """Write all of ``data`` or none of it.

    A write that fails part-way (ENOSPC mid-record) would leave a fragment with
    no newline that fuses with the next record; the file is cut back to where
    this write started before the error goes on. The lock is held, so nothing
    else has moved the end in between.
    """
    start = os.fstat(fd).st_size
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    except OSError:
        try:
            os.ftruncate(fd, start)
        except OSError:
            pass
        raise


def _rotated(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _rotate_if_full(path: Path, incoming: int) -> None:
    """Shift the backups and rename the live file aside when ``incoming`` bytes would pass the cap.

    Called with the lock held, so the size read here is the size written to.
    """
    try:
        size = os.stat(path).st_size
    except FileNotFoundError:
        return
    if size == 0 or size + incoming <= AUDIT_FILE_MAX_BYTES:
        return
    try:
        for index in range(AUDIT_FILE_BACKUP_COUNT, 1, -1):
            older = _rotated(path, index - 1)
            if older.exists():
                os.replace(older, _rotated(path, index))
        os.replace(path, _rotated(path, 1))
    except OSError:
        # A backup that cannot be moved must not cost the record: the write
        # goes ahead into whatever file is live.
        pass
