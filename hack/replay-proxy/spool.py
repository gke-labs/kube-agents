"""The proxy's files on disk: its log lines in closed segments, and a size budget.

A CI job copies the proxy's output to a private bucket while the job runs
(hack/replay-proxy/collect_shadow.py). Reading the lines back from
`kubectl logs` loses them: the kubelet rotates a container log at a few MiB and
`kubectl logs` returns only the newest file, and a pod that is replaced takes
its log with it. So each line also goes to a file under `REPLAY_LOG_DIR`:

- the line is appended to an open segment, `<prefix>-<seq>.jsonl.open`, and
  flushed at once;
- the segment is closed (renamed to `<prefix>-<seq>.jsonl`) once it is
  SEGMENT_MAX_BYTES long or has been open for the configured age, so a closed
  segment never changes again and the collector copies each one once;
- at start, a segment a previous run of the container left open is closed,
  without a trailing partial line, so a container restart loses nothing it had
  written.

`<prefix>` is the pod name and the start time in milliseconds, so two pods or
two starts of one container never write the same name.

DataBudget bounds what the proxy writes to its data volume: once the files
under its roots reach `max_bytes`, the request store and the segment log stop
writing (the line still goes to stdout), so a bucket upload that never
succeeds fills no volume and gets no pod evicted. The collector empties each
file it has uploaded, which is what keeps the total under the budget in a run
whose uploads work.
"""

from __future__ import annotations

import os
import re
import socket
import threading
import time
from typing import Callable, Iterable, List, Optional

OPEN_SUFFIX = ".open"
SEGMENT_SUFFIX = ".jsonl"
SEQ_DIGITS = 6
MS_PER_SECOND = 1000
NEWLINE = b"\n"
TEXT_ENCODING = "utf-8"
# Small enough that one upload of a segment is quick, large enough that a busy
# job writes a few hundred of them, not tens of thousands.
SEGMENT_MAX_BYTES = 8 * 1024 * 1024
# Owner only: the lines hold hashes that can be guessed against.
SEGMENT_FILE_MODE = 0o600
# Anything but these in a pod name becomes `_` in a file name.
UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9.-]")
SAFE_NAME_FILLER = "_"


def segment_prefix(hostname: Optional[str] = None, now: Optional[float] = None) -> str:
    """`<pod name>-<start ms>`, the part of a segment name unique to one run of the proxy."""
    host = UNSAFE_NAME_CHARS.sub(SAFE_NAME_FILLER, hostname if hostname is not None else socket.gethostname())
    started = int((now if now is not None else time.time()) * MS_PER_SECOND)
    return f"{host}-{started}"


def close_open_segments(directory: str) -> int:
    """Close every segment left open under `directory`; returns how many were kept.

    A trailing line without its newline was cut off mid-write and is dropped; a
    segment with nothing before it is removed. A segment that cannot be read or
    renamed is left as it is: the collector's last pass still takes its whole
    lines.
    """
    kept = 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(SEGMENT_SUFFIX + OPEN_SUFFIX):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "rb+") as handle:
                data = handle.read()
                end = data.rfind(NEWLINE) + 1
                if end != len(data):
                    handle.truncate(end)
            if end == 0:
                os.unlink(path)
                continue
            os.replace(path, path[: -len(OPEN_SUFFIX)])
        except OSError:
            continue
        kept += 1
    return kept


def tree_bytes(roots: Iterable[str]) -> int:
    """Bytes in the regular files under each root; a file that vanishes mid-walk counts nothing."""
    total = 0
    stack: List[str] = [r for r in roots if r]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


class DataBudget:
    """Allows writes while the files under `roots` hold less than `max_bytes`.

    Measured on a background thread every `interval_seconds`, and counted up
    by `note` between measurements, so a burst between two walks is not free.
    """

    def __init__(self, roots: Iterable[str], max_bytes: int, interval_seconds: float):
        self.roots = [r for r in roots if r]
        self.max_bytes = max_bytes
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._used = tree_bytes(self.roots)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="replay-shadow-budget", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            used = tree_bytes(self.roots)
            with self._lock:
                self._used = used

    def used(self) -> int:
        with self._lock:
            return self._used

    def allows(self) -> bool:
        return self.used() < self.max_bytes

    def note(self, written: int) -> None:
        with self._lock:
            self._used += written

    def close(self) -> None:
        self._stop.set()
        self._thread.join()


class SegmentLog:
    """Appends lines to the open segment and closes it by size or age (module docstring).

    Writes go straight to the file descriptor (no buffer), so a failed write
    can be cut back to the last whole line before the segment is closed.
    """

    def __init__(
        self, directory: str, prefix: str, max_age_seconds: float, budget: Optional[DataBudget] = None,
        max_bytes: int = SEGMENT_MAX_BYTES, clock: Callable[[], float] = time.monotonic,
    ):
        os.makedirs(directory, exist_ok=True)
        self.directory = directory
        self.prefix = prefix
        self.max_age_seconds = max_age_seconds
        self.max_bytes = max_bytes
        self.budget = budget
        self.clock = clock
        self.recovered = close_open_segments(directory)
        self._lock = threading.Lock()
        self._seq = 0
        self._fd: Optional[int] = None
        self._path = ""
        self._bytes = 0
        self._opened = 0.0

    def write(self, line: str) -> bool:
        """Append one line; False when the budget is spent and the line was not written.

        Raises OSError on a failed write, after cutting the segment back to its
        last whole line and closing it.
        """
        data = line.encode(TEXT_ENCODING) + NEWLINE
        with self._lock:
            if self.budget is not None and not self.budget.allows():
                return False
            if self._fd is None:
                self._open()
            before = self._bytes
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(self._fd, view):]  # type: ignore[arg-type]
            except OSError:
                self._abandon(before)
                raise
            self._bytes += len(data)
            if self.budget is not None:
                self.budget.note(len(data))
            if self._bytes >= self.max_bytes:
                self._close()
            return True

    def roll_if_due(self) -> None:
        """Close the open segment once it has been open for max_age_seconds."""
        with self._lock:
            if self._fd is not None and self.clock() - self._opened >= self.max_age_seconds:
                self._close()

    def close(self) -> None:
        with self._lock:
            if self._fd is not None:
                self._close()

    def _open(self) -> None:
        self._seq += 1
        name = f"{self.prefix}-{self._seq:0{SEQ_DIGITS}d}{SEGMENT_SUFFIX}{OPEN_SUFFIX}"
        self._path = os.path.join(self.directory, name)
        self._fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, SEGMENT_FILE_MODE)
        self._bytes = 0
        self._opened = self.clock()

    def _close(self) -> None:
        """Close the open segment: renamed to its closed name, or removed when empty.

        The descriptor is released first, so a failed rename leaves an `.open`
        file for the next start (close_open_segments) and a fresh segment for
        the next line, never a descriptor still pointing at a renamed file.
        """
        fd, self._fd = self._fd, None
        os.close(fd)  # type: ignore[arg-type]
        if self._bytes:
            os.replace(self._path, self._path[: -len(OPEN_SUFFIX)])
        else:
            os.unlink(self._path)

    def _abandon(self, good_bytes: int) -> None:
        """After a failed write: keep the whole lines before it, close the segment, never raise."""
        fd, self._fd = self._fd, None
        self._bytes = good_bytes
        try:
            os.ftruncate(fd, good_bytes)  # type: ignore[arg-type]
            truncated = True
        except OSError:
            truncated = False
        try:
            os.close(fd)  # type: ignore[arg-type]
        except OSError:
            pass
        try:
            if not truncated:
                # Left open: the next start cuts it to its last whole line.
                return
            if good_bytes:
                os.replace(self._path, self._path[: -len(OPEN_SUFFIX)])
            else:
                os.unlink(self._path)
        except OSError:
            pass
