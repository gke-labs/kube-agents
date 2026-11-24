"""Cron runs and skipped slots by job, read from every profile's execution ledger at scrape time.

Hermes runs the cron rosters, and keeps a ledger of every attempt in each
profile's ``cron/executions.db`` (upstream ``cron/executions.py``): one row a
run, from ``claimed`` through ``running`` to a terminal ``completed``,
``failed`` or ``unknown``, and one row for a slot that was skipped, with its
reason. The gateway process holds the metrics listener and sees the volume,
so the two families here are a read of those ledgers, the way
``KanbanGaveUpCounter`` is a read of the board.

The families are the roster's counterpart of the pair the operator serves for
an ``AgentSchedule`` (``kubeagents_schedule_runs_total`` and
``kubeagents_schedule_skips_total``), which replaces the rosters, and a run's
outcome uses that counter's words: ``succeeded``, ``failed`` or ``lost``,
Hermes' ``unknown`` being a run whose process went away before it recorded an
end.

A ledger is followed, not counted. Hermes keeps only the newest rows of each
job, so the size of the table says nothing about how many runs there were.
Each ledger has a low-water mark, the rowid of the oldest run still in flight
(else the one after the newest row), and a read takes only the rows from there
on. Hermes never changes a terminal row, so a row is counted the first time it
is seen terminal, and the rowids counted at or after the mark are kept so that
none is counted twice. Rows terminal at the first read are a baseline and
count nothing; a run in flight then counts when it ends. So both families
start at zero in each process, like the other counters here.

A series is served from zero. Each job met in a ledger, at the baseline too,
gets its three run outcomes at 0, and each (job, reason) met among the skipped
rows gets a 0, before anything is added: a rate over a counter cannot see a
series whose first sample is already 1, and the first failed run of a job
after a restart is the one an alert is for. A reason a job has never been
skipped for has no series until its first skip.

The order followed is the table's own, its rowid, not ``claimed_at``. Hermes
writes that timestamp in the profile's time zone and takes it before it has
the write lock, so its text is in time order only at one fixed offset and a
row can commit after a later-stamped one. A rowid is given at the insert, and
only grows while the newest row stays, which Hermes' pruning leaves. A table
whose newest rowid is below the mark was emptied or rebuilt, which numbers
its rows again from the start: the ledger takes a new baseline, and the rows
terminal just then are not counted.

A run lost to a restart of this process counts as ``lost`` only when Hermes'
recovery marks it after the listener has taken its baseline; one marked
before is terminal at the baseline and counts nothing.

The labels are bounded. A job id that is not shaped like one, or a new one
once ``MAX_JOB_SERIES`` are held, counts under ``other``; so does a reason
that is not shaped like one, or a new one past ``MAX_REASON_SERIES``. The
ledger sits on a volume every process of the agent can write, so its rows are
data from an untrusted writer: a read takes at most ``MAX_ROWS_PER_READ`` rows
and carries on after the last one, takes no more of a value than a label can
be long, and never raises into the scrape.
A read that fills that limit moves the mark past its last row even when a run
before it is still in flight, whose end is then not counted: the alternative
is a ledger whose newer rows are never reached.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import stat
import threading
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

from chat_metrics.volume import hermes_root, home_gone, profile_homes

logger = logging.getLogger("hermes.plugin.chat_metrics")

CRON_RUNS_METRIC = "kubeagents_cron_runs_total"
CRON_RUNS_HELP = (
    "Cron runs that ended since this process started, by job and outcome (succeeded, failed, lost), "
    "across the agent's profiles, read from their cron execution ledgers at scrape time."
)
CRON_SKIPS_METRIC = "kubeagents_cron_skips_total"
CRON_SKIPS_HELP = (
    "Cron slots that did not run since this process started, by job and reason, "
    "across the agent's profiles, read from their cron execution ledgers at scrape time."
)
# Not `job`: Prometheus gives every scraped series a `job` label of its own
# (the scrape job), and a collector renames a series label that collides with it.
JOB_LABEL = "cron_job"
OUTCOME_LABEL = "outcome"
REASON_LABEL = "reason"
LABEL_OTHER = "other"
# Where a profile's ledger is under its home (upstream cron/executions.py).
CRON_DIR_NAME = "cron"
LEDGER_FILE_NAME = "executions.db"
# The ledger's terminal run states and the outcome each is served as: the
# AgentSchedule counter's words. A status outside both maps is a run still in
# flight (claimed, running).
RUN_OUTCOMES = {"completed": "succeeded", "failed": "failed", "unknown": "lost"}
SKIPPED_STATUS = "skipped"
# The reason of a skipped row that carries none (SKIP_REASON_UNSPECIFIED in
# deploy/docker/patches/cron_skip_ledger.py, the patch that records skips).
REASON_UNSPECIFIED = "unspecified"
# A roster's job ids are kebab-case words; a job created on request has an id
# Hermes makes. Letters, digits and single `-` or `_` between them.
JOB_ID_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")
JOB_ID_MAX_LENGTH = 64
# Distinct job labels one process holds before a new one counts as other. The
# two shipped rosters hold about 30 jobs.
MAX_JOB_SERIES = 200
# The skip ledger's reasons are snake_case words: a test here holds this shape,
# and MAX_REASON_SERIES, to SKIP_REASONS in deploy/docker/patches/cron_skip_ledger.py.
REASON_RE = re.compile(r"[a-z]+(?:_[a-z]+)*")
REASON_MAX_LENGTH = 48
MAX_REASON_SERIES = 32
# A read waits this long on a locked ledger before keeping the last counts.
LEDGER_BUSY_TIMEOUT_SECONDS = 1.0
# The most rows one read takes from one ledger. Hermes keeps at most 1,000
# rows a job, so a healthy read is far under this; a larger table is read over
# several scrapes.
MAX_ROWS_PER_READ = 20_000
# The longest status the ledger's CHECK allows is nine characters.
STATUS_MAX_LENGTH = 16
# Rows from the low-water mark on, in the table's own order, so a read cut at
# the limit carries on after its last row. Each value is cut in SQL to one
# character more than its label may be long, so an oversized one still reads
# as too long and is never loaded whole.
_ROWS_SQL = (
    f"SELECT rowid, substr(job_id, 1, {JOB_ID_MAX_LENGTH + 1}), substr(status, 1, {STATUS_MAX_LENGTH}), "
    f"substr(skip_reason, 1, {REASON_MAX_LENGTH + 1}) FROM executions WHERE rowid >= ? ORDER BY rowid LIMIT ?"
)
_NEWEST_SQL = "SELECT max(rowid) FROM executions"
# Both statements of a read run in one transaction, so they see one snapshot.
_BEGIN_SQL = "BEGIN"
# SQLite's URI form of a path, opened read-only: the listener never writes a ledger.
_READ_ONLY_URI = "file:{path}?mode=ro"
# Before any row has been seen: every rowid is at or after it.
_NO_MARK = 0


def ledger_path(home: Path) -> Path:
    """``<home>/cron/executions.db``."""
    return home / CRON_DIR_NAME / LEDGER_FILE_NAME


def ledger_files(root: Path) -> list[Path]:
    """The ledger of the front door and of each named profile under ``root``, existing or not.

    Raises OSError when the profiles directory is there and cannot be listed.
    """
    return [ledger_path(home) for home in profile_homes(root)]


def job_label(job_id: Any) -> str:
    """The label for a ledger row's job id: the id when it is shaped like one, else other."""
    if not isinstance(job_id, str) or len(job_id) > JOB_ID_MAX_LENGTH or not JOB_ID_RE.fullmatch(job_id):
        return LABEL_OTHER
    return job_id


def reason_label(reason: Any) -> str:
    """The label for a skipped row's reason: unspecified when it has none, the reason when shaped like one, else other."""
    if reason is None or reason == "":
        return REASON_UNSPECIFIED
    if not isinstance(reason, str) or len(reason) > REASON_MAX_LENGTH or not REASON_RE.fullmatch(reason):
        return LABEL_OTHER
    return reason


class _Ledger:
    """Where the read of one ledger stands: which file it is, its low-water mark, the rowids seen terminal at or after it, and whether it is still taking its baseline."""

    __slots__ = ("identity", "mark", "counted", "baselining")

    def __init__(self, identity: tuple[int, int], baselining: bool) -> None:
        # The file's device and inode. A mark is a position in one file: a
        # profile removed and made again has a ledger of the same name that
        # numbers its rows from 1.
        self.identity = identity
        self.mark = _NO_MARK
        self.counted: set[int] = set()
        # True until a read reaches the end of the table: the rows terminal
        # until then were there before the count began.
        self.baselining = baselining


class CronRunsCounter:
    """Cron runs by (job, outcome) and skipped slots by (job, reason) since the first read of the ledgers.

    The first read is the baseline: the rows terminal then count nothing
    (``ChatMetrics.prime`` makes it, so the baseline is the listener's start).
    A ledger that appears later, a new profile's, counts every row it holds;
    one that could not be read at the baseline takes its baseline when it
    first can be, and so does every ledger while the profiles directory has
    not yet been listed. A ledger that cannot be read keeps its mark and is
    tried again at the next read; the failure is logged once per streak. A
    ledger whose profile is removed is forgotten, and one whose file is not the
    file last read (its device and inode differ) is a new ledger. A file system
    that gives the new file the old one's inode hides that change; the table's
    newest rowid falling under the mark then catches most of it.
    :meth:`counts` does not raise.
    """

    def __init__(
        self,
        root: Callable[[], Path] = hermes_root,
        max_jobs: int = MAX_JOB_SERIES,
        max_reasons: int = MAX_REASON_SERIES,
        max_rows: int = MAX_ROWS_PER_READ,
    ) -> None:
        self._root = root
        self._max_jobs = max_jobs
        self._max_reasons = max_reasons
        self._max_rows = max_rows
        self._lock = threading.Lock()
        self._ledgers: dict[Path, _Ledger] = {}
        self._runs: dict[tuple[str, str], int] = {}
        self._skips: dict[tuple[str, str], int] = {}
        self._jobs: set[str] = set()
        self._reasons: set[str] = set()
        self._baselined = False
        # Ledgers the baseline read could not open: their history is not new.
        self._unbaselined: set[Path] = set()
        self._failing = False

    @staticmethod
    def _bounded(label: str, held: set[str], limit: int) -> str:
        """``label``, or other once ``held`` has ``limit`` other labels and this is a new one."""
        if label == LABEL_OTHER or label in held:
            return label
        if len(held) >= limit:
            return LABEL_OTHER
        held.add(label)
        return label

    def _job(self, job_id: Any) -> str:
        """The label for ``job_id``, with its three run outcomes served from zero the first time it is met."""
        job = self._bounded(job_label(job_id), self._jobs, self._max_jobs)
        for outcome in RUN_OUTCOMES.values():
            self._runs.setdefault((job, outcome), 0)
        return job

    def _count(self, job_id: Any, status: str, reason: Any, add: bool) -> None:
        """Serve the series of a terminal row from zero, and add the row to it when ``add``."""
        job = self._job(job_id)
        if status == SKIPPED_STATUS:
            key = (job, self._bounded(reason_label(reason), self._reasons, self._max_reasons))
            self._skips[key] = self._skips.setdefault(key, 0) + add
        else:
            key = (job, RUN_OUTCOMES[status])
            self._runs[key] += add

    def _rows(self, path: Path, mark: int) -> tuple[Optional[int], list[tuple[Any, ...]]]:
        """The table's newest rowid (None for an empty table) and its rows from ``mark`` on, read in one snapshot."""
        # SQLite reads the path as a URI, so `?`, `#` and `%` in it are encoded;
        # `/` is left alone and SQLite decodes the rest back.
        conn = sqlite3.connect(_READ_ONLY_URI.format(path=quote(str(path))), uri=True, timeout=LEDGER_BUSY_TIMEOUT_SECONDS)
        try:
            conn.execute(_BEGIN_SQL)
            newest = conn.execute(_NEWEST_SQL).fetchone()[0]
            if isinstance(newest, int) and newest < mark - 1:
                mark = _NO_MARK  # the table was rebuilt under the mark: read it from its start
            return newest, conn.execute(_ROWS_SQL, (mark, self._max_rows)).fetchall()
        finally:
            conn.close()

    def _read_ledger(self, path: Path) -> None:
        try:
            status = os.stat(path)
        except FileNotFoundError:
            return  # a profile with no cron run yet
        if not stat.S_ISREG(status.st_mode):
            return
        identity = (status.st_dev, status.st_ino)
        ledger = self._ledgers.get(path)
        if ledger is not None and ledger.identity != identity:
            # Another file under the same name: the profile was removed and made
            # again, whether or not a read saw it gone. It is a ledger that
            # appears later, and counts every row it holds.
            del self._ledgers[path]
            ledger = None
        newest, rows = self._rows(path, ledger.mark if ledger is not None else _NO_MARK)
        if ledger is None:
            ledger = self._ledgers[path] = _Ledger(identity, baselining=not self._baselined or path in self._unbaselined)
            self._unbaselined.discard(path)
        elif isinstance(newest, int) and newest < ledger.mark - 1:
            # Rebuilt: the rowids kept no longer name the same rows, so what is
            # terminal now is a baseline again.
            ledger.mark, ledger.counted, ledger.baselining = _NO_MARK, set(), True
        terminal: list[int] = []  # rowids of the terminal rows read
        in_flight: Optional[int] = None  # rowid of the oldest row still in flight
        last = ledger.mark - 1
        for rowid, job_id, status, reason in rows:
            if not isinstance(rowid, int):
                continue
            last = rowid
            if status == SKIPPED_STATUS or status in RUN_OUTCOMES:
                terminal.append(rowid)
                self._count(job_id, status, reason, add=not ledger.baselining and rowid not in ledger.counted)
            else:
                self._job(job_id)
                if in_flight is None:
                    in_flight = rowid
        cut = len(rows) >= self._max_rows
        # The next read starts at the oldest run in flight, else after the last
        # row read; a read cut at the limit moves on past its last row whatever
        # is in flight. The rowids kept are the terminal rows that read will see again.
        ledger.mark = last + 1 if cut or in_flight is None else in_flight
        ledger.counted = {rowid for rowid in terminal if rowid >= ledger.mark}
        if not cut:
            ledger.baselining = False

    def counts(self) -> tuple[dict[tuple[str, str], int], dict[tuple[str, str], int]]:
        """Runs by (job, outcome) and skips by (job, reason), after reading what the ledgers gained since the last call."""
        with self._lock:
            failed: Optional[BaseException] = None
            listed = True
            try:
                files = ledger_files(self._root())
            except Exception as exc:  # noqa: BLE001 - a scrape serves the other families whatever this read meets
                # The ledgers already followed are still read; which others exist is not known.
                files, failed, listed = list(self._ledgers), exc, False
            for path in files:
                try:
                    self._read_ledger(path)
                except Exception as exc:  # noqa: BLE001 - see above; the ledger keeps its mark for the next read
                    failed = exc
                    if not self._baselined:
                        self._unbaselined.add(path)
            # A profile that was removed: a ledger of that name later is a new
            # profile's, and counts every row it holds.
            for path in [known for known in self._ledgers if known not in files and home_gone(known.parent.parent)]:
                del self._ledgers[path]
            # The baseline is taken from a read that saw which ledgers exist: until
            # one does, a ledger met for the first time takes a baseline of its own.
            self._baselined = self._baselined or listed
            if failed is not None and not self._failing:
                logger.warning("chat_metrics: a cron execution ledger could not be read, keeping the last counts: %s", type(failed).__name__)
            self._failing = failed is not None
            return dict(self._runs), dict(self._skips)
