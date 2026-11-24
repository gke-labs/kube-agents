"""Skills an agent loaded on demand, by skill, read from every profile's audit file at scrape time.

An agent loads a skill on demand by calling Hermes' ``skill_view`` tool with
the skill's name. Most of those calls happen in processes nothing scrapes: a kanban worker
or a Cluster Agent profile, each a short-lived process of its own. What they
share is the ``tool_call_audit`` plugin, which every profile enables and which
appends one ``tool_call_start`` record per tool call to the profile's
``logs/audit.jsonl`` on the data volume (``common/audit_sink.py``). The gateway
process holds the metrics listener and sees that volume, so the count is a read
of those files, the way ``KanbanGaveUpCounter`` is a read of the board.

What counts: a ``skill_view`` tool call that opens a skill itself, each time
it is made. A call with a ``file_path`` opens one of the skill's reference
documents, which the agent does after it has the skill, so it is the same use
and is not counted. The count is of requests: a call that names a skill the
profile does not have counts too, under the name it gave.

What does not: a skill Hermes puts in the prompt without a tool call. A cron
job's ``skills`` field, a kanban task's ``--skills`` and a slash command each
call ``tools.skills_tool.skill_view`` as a function, outside tool dispatch, so
no ``pre_tool_call`` hook runs and no record is written. The scheduled fleet
audits load ``fleet-audit`` that way. Nor a record that never reached the
file: one the sink sent to stdout instead, or one rotated past the last backup
between two reads.

The files are followed, not re-read. Each has a cursor, the inode and the
offset of the last whole line read, and a scrape reads only what was appended
since. The sink rotates a full file to ``.1`` (then ``.2``, ``.3``); a cursor
whose inode is no longer the live file's is finished from the backup that
holds that inode, and every newer backup is read whole, before the live file
starts from zero. Files present at the first read are a baseline and count
nothing, so the family starts at zero in each process, like the other
counters here.

The label is bounded, because the name is whatever the model passed: a value
that is not shaped like a skill name, or a new name once ``MAX_SKILL_SERIES``
are held, counts under ``other``.

A read never raises into the scrape. The files sit on a volume every process
of the agent can write, so a line is data from an untrusted writer: one longer
than ``MAX_RECORD_BYTES`` is skipped unparsed, and a line the parser fails on
in any way is skipped. The cursor moves past each line as it is read, so a
failure part-way through a file neither repeats the lines before it nor meets
the same line again.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, BinaryIO, Callable, Optional

from common import audit_sink
from common.audit_schema import EVENT_TYPE_KEY

logger = logging.getLogger("hermes.plugin.chat_metrics")

SKILL_INVOCATIONS_METRIC = "kubeagents_skill_invocations_total"
SKILL_INVOCATIONS_HELP = (
    "skill_view tool calls that opened a skill since this process started, by skill, across the agent's profiles, "
    "read from their audit files at scrape time; a skill a cron job or a task preloads is not a tool call and is not counted."
)
SKILL_LABEL = "skill"
SKILL_LABEL_OTHER = "other"
# The tool and the record the tool_call_audit plugin writes when a call starts
# (tool_call_audit/audit.py's log_pre_tool_call), and the fields read from it.
SKILL_VIEW_TOOL = "skill_view"
TOOL_CALL_START_EVENT = "tool_call_start"
TOOL_FIELD = "tool"
ARGS_FIELD = "args"
SKILL_NAME_ARG = "name"
SKILL_FILE_PATH_ARG = "file_path"
# A line that does not hold the tool's name as a JSON string is not parsed:
# most records are other tools' calls.
SKILL_VIEW_MARKER = b'"skill_view"'
# The Agent Skills name rule (lowercase letters, digits and hyphens) for each
# segment, in the two longer forms Hermes' skill_view also takes: `/` between
# segments for a skill addressed under its category, and a `plugin:` prefix for
# a skill a plugin ships.
_SKILL_SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"
SKILL_NAME_RE = re.compile(rf"(?:{_SKILL_SEGMENT}:)?{_SKILL_SEGMENT}(?:/{_SKILL_SEGMENT})*")
# The Agent Skills limit on a name is 64 characters; twice that leaves room
# for a category or a plugin.
SKILL_NAME_MAX_LENGTH = 128
# Distinct skill labels one process holds before a new name counts as other.
# The images ship about 70 skills across the profiles.
MAX_SKILL_SERIES = 200
LINE_END = b"\n"
# A tool_call_start record is a few hundred bytes: the plugin cuts its `args`
# at 2,000 characters (tool_call_audit/audit.py's _PAYLOAD_LOG_LIMIT). A line
# far past that is not the plugin's, and is skipped without being parsed.
MAX_RECORD_BYTES = 64 * 1024
_READ_MODE = "rb"


def hermes_root() -> Path:
    """The data volume's Hermes home, whose ``logs/`` and ``profiles/*/logs/`` hold the audit files."""
    return Path(os.environ.get(audit_sink.HERMES_HOME_ENV) or audit_sink.DEFAULT_HERMES_HOME)


def audit_files(root: Path) -> list[Path]:
    """The live audit file of the front door and of each named profile under ``root``, existing or not."""
    files = [audit_sink.audit_file_path(root)]
    try:
        profiles = sorted(entry for entry in (root / audit_sink.PROFILES_DIR_NAME).iterdir() if entry.is_dir())
    except OSError:
        return files
    return files + [audit_sink.audit_file_path(profile) for profile in profiles]


def profile_home(audit_file: Path) -> Path:
    """The profile home an audit file path belongs to: the directory above its ``logs/``."""
    return audit_file.parent.parent


def profile_gone(audit_file: Path) -> bool:
    """Whether the profile an audit file belongs to has been removed; False when that cannot be told."""
    try:
        os.stat(profile_home(audit_file))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def rotated(path: Path, index: int) -> Path:
    """The ``index``-th backup of the audit file at ``path``, as the sink names it."""
    return path.with_name(f"{path.name}.{index}")


def skill_label(name: Any) -> str:
    """The label for a ``skill_view`` name: the name when it is shaped like one, else other."""
    if not isinstance(name, str) or len(name) > SKILL_NAME_MAX_LENGTH or not SKILL_NAME_RE.fullmatch(name):
        return SKILL_LABEL_OTHER
    return name


def skill_loaded(line: bytes) -> Optional[str]:
    """The label of the skill ``line`` records a load of, or None when it records none."""
    if len(line) > MAX_RECORD_BYTES or SKILL_VIEW_MARKER not in line:
        return None
    try:
        record = json.loads(line)
    except (ValueError, RecursionError):  # RecursionError: a value nested too deep to parse
        return None
    if not isinstance(record, dict):
        return None
    if record.get(EVENT_TYPE_KEY) != TOOL_CALL_START_EVENT or record.get(TOOL_FIELD) != SKILL_VIEW_TOOL:
        return None
    # The plugin writes the arguments as a JSON string, cut short when long. A
    # call whose arguments cannot be read was still a call: it counts as other.
    args = record.get(ARGS_FIELD)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, RecursionError):
            return SKILL_LABEL_OTHER
    if not isinstance(args, dict):
        return SKILL_LABEL_OTHER
    if args.get(SKILL_FILE_PATH_ARG):
        return None
    return skill_label(args.get(SKILL_NAME_ARG))


class _Cursor:
    """Where the read of one audit file stands: the inode read, and the offset after its last whole line."""

    __slots__ = ("inode", "offset")

    def __init__(self, inode: int, offset: int) -> None:
        self.inode = inode
        self.offset = offset


class SkillInvocationsCounter:
    """``skill_view`` loads by skill since the first read of the audit files.

    The first read is the baseline: each file there then starts at its end and
    counts nothing (``ChatMetrics.prime`` makes it, so the baseline is the
    listener's start). A file that appears later, a new profile's, is read
    from its start; one that could not be read at the baseline starts at its
    end when it first can be. A file that cannot be read keeps its cursor and
    is tried again at the next read; the failure is logged once per streak. A
    cursor is dropped only when its profile's directory is gone, never because
    a listing failed. :meth:`counts` does not raise.
    """

    def __init__(self, root: Callable[[], Path] = hermes_root, max_series: int = MAX_SKILL_SERIES) -> None:
        self._root = root
        self._max_series = max_series
        self._lock = threading.Lock()
        self._cursors: dict[Path, _Cursor] = {}
        self._counts: dict[str, int] = {}
        self._baselined = False
        # Files the baseline read could not open: their history is not new.
        self._unbaselined: set[Path] = set()
        self._failing = False

    def _count(self, label: str) -> None:
        if label not in self._counts and label != SKILL_LABEL_OTHER and len(self._counts) >= self._max_series:
            label = SKILL_LABEL_OTHER
        self._counts[label] = self._counts.get(label, 0) + 1

    def _read_lines(self, handle: BinaryIO, cursor: _Cursor) -> None:
        """Count the whole lines of ``handle`` from the cursor's offset, moving the cursor past each as it is read."""
        handle.seek(cursor.offset)
        for line in handle:
            if not line.endswith(LINE_END):
                break  # a record still being written: read it when it is whole
            cursor.offset += len(line)
            label = skill_loaded(line)
            if label is not None:
                self._count(label)

    def _finish_rotated(self, path: Path, cursor: _Cursor, live_inode: int) -> None:
        """Read what the live file's predecessors hold past ``cursor``: the backup with its inode, then each newer one.

        ``live_inode`` is the file the caller holds open as the live one. A
        rotation between that open and this call makes it a backup too; it is
        left to the caller's own read, so it is not read twice.
        """
        held: Optional[int] = None
        for index in range(1, audit_sink.AUDIT_FILE_BACKUP_COUNT + 1):
            try:
                if os.stat(rotated(path, index)).st_ino == cursor.inode:
                    held = index
                    break
            except OSError:
                continue
        if held is None:
            return  # rotated past the last backup, or deleted: what it held is lost
        for index in range(held, 0, -1):
            try:
                with open(rotated(path, index), _READ_MODE) as handle:
                    inode = os.fstat(handle.fileno()).st_ino
                    if inode == live_inode:
                        continue
                    self._read_lines(handle, cursor if inode == cursor.inode else _Cursor(inode, 0))
            except OSError:
                continue

    def _read_file(self, path: Path) -> None:
        try:
            handle = open(path, _READ_MODE)
        except FileNotFoundError:
            return  # a profile with no record yet; a cursor waits for the file to come back
        with handle:
            status = os.fstat(handle.fileno())
            cursor = self._cursors.get(path)
            if cursor is None:
                at_end = not self._baselined or path in self._unbaselined
                cursor = self._cursors[path] = _Cursor(status.st_ino, status.st_size if at_end else 0)
                self._unbaselined.discard(path)
            elif cursor.inode != status.st_ino:
                self._finish_rotated(path, cursor, status.st_ino)
                cursor.inode, cursor.offset = status.st_ino, 0
            elif status.st_size < cursor.offset:
                cursor.offset = 0  # truncated in place
            self._read_lines(handle, cursor)

    def counts(self) -> dict[str, int]:
        """The loads by skill label, after reading what the audit files gained since the last call."""
        with self._lock:
            failed: Optional[BaseException] = None
            try:
                files = audit_files(self._root())
            except Exception as exc:  # noqa: BLE001 - a scrape serves the other families whatever this read meets
                files, failed = [], exc
            for path in files:
                try:
                    self._read_file(path)
                except Exception as exc:  # noqa: BLE001 - see above; the file keeps its cursor for the next read
                    failed = exc
                    if not self._baselined:
                        self._unbaselined.add(path)
            for path in [known for known in self._cursors if profile_gone(known)]:
                del self._cursors[path]
            self._baselined = True
            if failed is not None and not self._failing:
                logger.warning("chat_metrics: an audit file could not be read, keeping its last position: %s", type(failed).__name__)
            self._failing = failed is not None
            return dict(self._counts)
