#!/usr/bin/env python3
"""Stop the largest session process before the shell container is OOMKilled.

Every agent's terminal, script and `kubectl` runs in this one container. When it
reaches its memory limit the kernel OOMKills it, every session in it ends, and the
agents send the same work again. One `kubectl get ... -A -o json` of a cluster of
100,000 Pods holds about 14 GiB, seven times the default limit, so a single fleet
read is enough.

This process runs as root beside sshd, started by the entrypoint. Every
`INTERVAL_SECONDS` it reads the container's memory in use. At or above the high
watermark (the limit less `max(HEADROOM_FLOOR_BYTES, limit / HEADROOM_DIVISOR)`,
at most a quarter of the limit; under gVisor also less `GVISOR_PER_PROCESS_BYTES`
per process) it sends SIGKILL to candidates, largest first, in one batch that
brings usage `BATCH_MARGIN_BYTES` under the watermark: a candidate is a
process of a non-root user holding at least `MIN_VICTIM_BYTES` of the memory a
kill frees (its `RssAnon` plus `RssShmem`, or `VmRSS` where the kernel does not
report them). Memory of a process signalled in the last `SETTLE_SECONDS` counts
as already leaving. Sessions run as non-root users, so the agents' commands
are candidates; sshd, the entrypoint and this guard run as root and are not, and a
session's shell or its sshd process is far below the size floor. The stopped
commands fail; the container and every other session stay up. Many commands each
under the floor are not stopped; the kubectl admission's slots bound how many run.

Memory in use is what the kernel cannot reclaim before it OOMKills: the cgroup's
usage less its page cache (shared memory excepted) and its reclaimable kernel
slab. cgroup v2: `memory.current` less `file`, plus `shmem`, less
`slab_reclaimable`. cgroup v1 (what GKE Sandbox exposes): `memory.usage_in_bytes`
less `total_cache`, plus `total_shmem`. A missing `memory.stat` subtracts nothing.

The limit is `SANDBOX_MEMORY_LIMIT_BYTES`, which the operator sets from the
container's `limits.memory` because gVisor's cgroup v1 limit can be the sandbox's
virtual memory, else cgroup v2 `memory.max`, else cgroup v1
`memory.limit_in_bytes` when it is below the machine's `MemTotal`. While either the usage or the limit
cannot be read, the guard stops nothing, and says so when that starts.

Before each kill the guard checks that a kill can help. When the memory held
outside processes (usage less the summed sizes of every process) is so
large that stopping the smallest candidate could not keep the container under its
limit, the memory is in tmpfs or a mapping no kill frees, and killing would only
empty the container of sessions: the guard holds off while that holds, and kills
again as soon as a kill can keep the container under its limit, or once usage falls
below the watermark. Sizes count copy-on-write and shared pages in every process
that maps them (and gVisor reports only `VmRSS`), so the outside figure errs
towards killing. Over the watermark with no candidate (many commands each under the
floor), it says so once, then waits.

Each kill, hold, release and "no candidate" is one JSON line on stderr, which is
the container's log. A kill names the command by its program and the plain words
before its first flag. A flag and its value, a long word, and a word with a digit or
with punctuation outside `.,_-/:` end what is kept, so a token passed by flag, in a URL,
or as a word holding a digit does not reach the log; a positional secret made only of
letters could.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

COMPONENT = "shell-memory-guard"
INTERVAL_SECONDS = 0.1
# The watermark sits this far below the limit. At the default 2 GiB limit, 256 MiB
# covers one interval of growth (kubectl grows 45 to 60 MiB/s) for about 40
# commands, more than the 28 candidates of MIN_VICTIM_BYTES that fit under it.
HEADROOM_FLOOR_BYTES = 256 * 1024 * 1024
HEADROOM_DIVISOR = 8
# The headroom is at most this fraction of the limit, so a small limit keeps most
# of itself usable (a 384 MiB watermark at 512 MiB, not 256 MiB) and a limit of
# 256 MiB or less keeps a positive one.
HEADROOM_CAP_DIVISOR = 4
# Under gVisor the host charges the pod for the sandbox's own kernel and file proxy as well
# as the guest's memory, and that overhead grows with the number of guest processes; the
# guest's counters do not show it. On gVisor the watermark also reserves this much per
# process. Measured: at about 180 processes the host OOMKilled a 2 GiB sandbox while the
# guest read 1,795 MiB, an overhead of at least 1.4 MiB a process; this is about twice that.
GVISOR_PER_PROCESS_BYTES = 3 * 1024 * 1024
GVISOR_RELEASE_MARK = "gvisor"
# The smallest process the guard stops. A session's shell, its sshd process and
# small tools are a few MiB; stopping them frees nothing and ends sessions.
MIN_VICTIM_BYTES = 64 * 1024 * 1024
# A killed process still holding memory counts as memory on its way out for this long;
# after that it is taken to be stuck in the kernel, and others are chosen in its place.
SETTLE_SECONDS = 1.0
# A batch of kills aims this far below the watermark, so a burst does not stop one
# process at a time while the others keep growing.
BATCH_MARGIN_BYTES = 64 * 1024 * 1024
LIMIT_ENV = "SANDBOX_MEMORY_LIMIT_BYTES"
CGROUP_ROOT = "/sys/fs/cgroup"
PROC_ROOT = "/proc"
CGROUP_V2_CURRENT = "memory.current"
CGROUP_V2_MAX = "memory.max"
CGROUP_V2_STAT = "memory.stat"
CGROUP_V2_FILE = "file"
CGROUP_V2_SHMEM = "shmem"
CGROUP_V2_SLAB_RECLAIMABLE = "slab_reclaimable"
CGROUP_V1_USAGE = "memory/memory.usage_in_bytes"
CGROUP_V1_STAT = "memory/memory.stat"
CGROUP_V1_CACHE = "total_cache"
CGROUP_V1_SHMEM = "total_shmem"
CGROUP_V1_LIMIT = "memory/memory.limit_in_bytes"
MEMINFO = "meminfo"
MEMINFO_TOTAL = "MemTotal:"
PROC_STATUS = "status"
PROC_CMDLINE = "cmdline"
STATUS_UID = "Uid"
STATUS_RSS = "VmRSS"
STATUS_RSS_ANON = "RssAnon"
STATUS_RSS_SHMEM = "RssShmem"
# What a kill log keeps of a command line: the program's name and at most this many
# following words, stopping at the first flag or the first word that is long, holds a digit
# or holds a character outside LOGGED_WORD_CHARS. Tokens and keys carry digits; a subcommand,
# a kind or a script path seldom does.
LOGGED_WORDS_MAX = 4
LOGGED_WORD_MAX = 48
LOGGED_WORD_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.,_-/:")
ROOT_UID = 0
KIB = 1024


def read_text(path: str) -> str | None:
    """A file's text, undecodable bytes replaced, or None if it cannot be read."""
    try:
        with open(path, "rb") as f:
            return f.read().decode(errors="replace").strip()
    except OSError:
        return None


def read_int(path: str) -> int | None:
    text = read_text(path)
    return int(text) if text is not None and text.isdecimal() else None


def stat_values(path: str) -> dict[str, int]:
    out = {}
    for line in (read_text(path) or "").splitlines():
        name, _, value = line.partition(" ")
        if value.strip().isdecimal():
            out[name] = int(value)
    return out


def memory_in_use(cgroup_root: str = CGROUP_ROOT) -> int | None:
    """Bytes the kernel cannot reclaim before an OOMKill, or None if no counter is readable."""
    current = read_int(os.path.join(cgroup_root, CGROUP_V2_CURRENT))
    if current is not None:
        s = stat_values(os.path.join(cgroup_root, CGROUP_V2_STAT))
        return (current - s.get(CGROUP_V2_FILE, 0) + s.get(CGROUP_V2_SHMEM, 0)
                - s.get(CGROUP_V2_SLAB_RECLAIMABLE, 0))
    usage = read_int(os.path.join(cgroup_root, CGROUP_V1_USAGE))
    if usage is not None:
        s = stat_values(os.path.join(cgroup_root, CGROUP_V1_STAT))
        return usage - s.get(CGROUP_V1_CACHE, 0) + s.get(CGROUP_V1_SHMEM, 0)
    return None


def mem_total(proc_root: str = PROC_ROOT) -> int | None:
    for line in (read_text(os.path.join(proc_root, MEMINFO)) or "").splitlines():
        if line.startswith(MEMINFO_TOTAL):
            value = line.split()[1]
            return int(value) * KIB if value.isdecimal() else None
    return None


def memory_limit(environ: dict[str, str], cgroup_root: str = CGROUP_ROOT, proc_root: str = PROC_ROOT) -> int | None:
    """The container's memory limit in bytes, or None if it has none the guard can read.

    The operator's variable first, then cgroup v2 `memory.max`, then cgroup v1's
    `memory.limit_in_bytes` only when it is below the machine's `MemTotal`: under gVisor
    that file is the container's limit on the shell and the sandbox's whole virtual memory
    elsewhere, and only the first is a limit to guard.
    """
    from_env = environ.get(LIMIT_ENV, "").strip()
    if from_env.isdecimal() and int(from_env) > 0:
        return int(from_env)
    text = read_text(os.path.join(cgroup_root, CGROUP_V2_MAX))
    if text and text.isdecimal():
        return int(text)
    v1 = read_int(os.path.join(cgroup_root, CGROUP_V1_LIMIT))
    total = mem_total(proc_root)
    if v1 and total and v1 < total:
        return v1
    return None


def high_watermark(limit: int) -> int:
    headroom = max(HEADROOM_FLOOR_BYTES, limit // HEADROOM_DIVISOR)
    return limit - min(headroom, limit // HEADROOM_CAP_DIVISOR)


def on_gvisor() -> bool:
    return GVISOR_RELEASE_MARK in os.uname().release.lower()


def process_count(proc_root: str = PROC_ROOT) -> int:
    try:
        return sum(1 for name in os.listdir(proc_root) if name.isdecimal())
    except OSError:
        return 0


def effective_watermark(limit: int, gvisor: bool, processes_now: int) -> int:
    """The watermark, less gVisor's per-process overhead on a gVisor sandbox."""
    return high_watermark(limit) - (GVISOR_PER_PROCESS_BYTES * processes_now if gvisor else 0)


def process(proc_root: str, pid: str) -> tuple[int, int, int, str] | None:
    """(pid, uid, bytes a kill frees, command line), or None if the process is gone or unreadable."""
    fields = {}
    for line in (read_text(os.path.join(proc_root, pid, PROC_STATUS)) or "").splitlines():
        key, _, value = line.partition(":")
        fields[key] = value.split()
    try:
        uid = int(fields[STATUS_UID][0])
        if fields.get(STATUS_RSS_ANON):
            rss = (int(fields[STATUS_RSS_ANON][0]) + int((fields.get(STATUS_RSS_SHMEM) or ["0"])[0])) * KIB
        else:
            rss = int(fields[STATUS_RSS][0]) * KIB if fields.get(STATUS_RSS) else 0
    except (KeyError, IndexError, ValueError):
        return None
    cmd = (read_text(os.path.join(proc_root, pid, PROC_CMDLINE)) or "").replace("\0", " ").strip()
    return int(pid), uid, rss, cmd


def processes(proc_root: str = PROC_ROOT) -> list[tuple[int, int, int, str]]:
    """Every process that can be read; one that vanishes or is malformed is skipped."""
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    return [p for p in (process(proc_root, n) for n in names if n.isdecimal()) if p is not None]


def is_candidate(uid: int, rss: int) -> bool:
    """A process the guard may stop: a session user's, and large enough that stopping it frees memory."""
    return uid != ROOT_UID and rss >= MIN_VICTIM_BYTES


def choose_victim(procs: list[tuple[int, int, int, str]]) -> tuple[int, int, str] | None:
    """(pid, resident bytes, command line) of the largest candidate, or None."""
    pool = [(rss, pid, cmd) for pid, uid, rss, cmd in procs if is_candidate(uid, rss)]
    if not pool:
        return None
    rss, pid, cmd = max(pool)
    return pid, rss, cmd


def loggable_command(cmd: str) -> str:
    """The program and the plain words before its first flag: never a flag's value, a long word,
    or a word with a digit or punctuation."""
    words = cmd.split()
    if not words:
        return ""
    kept = [os.path.basename(words[0])[:LOGGED_WORD_MAX]]
    for word in words[1:1 + LOGGED_WORDS_MAX]:
        if word.startswith("-") or len(word) > LOGGED_WORD_MAX or not set(word) <= LOGGED_WORD_CHARS:
            break
        kept.append(word)
    return " ".join(kept)


def choose_victims(procs: list[tuple[int, int, int, str]], excess: int) -> list[tuple[int, int, str]]:
    """The largest candidates, largest first, until their memory covers `excess` (at least one)."""
    pool = sorted(((rss, pid, cmd) for pid, uid, rss, cmd in procs if is_candidate(uid, rss)), reverse=True)
    out, freed = [], 0
    for rss, pid, cmd in pool:
        out.append((pid, rss, cmd))
        freed += rss
        if freed >= excess:
            break
    return out


def over_watermark(used: int | None, limit: int | None) -> bool:
    return used is not None and limit is not None and used >= high_watermark(limit)


def log(record: dict) -> None:
    sys.stderr.write(json.dumps({"component": COMPONENT, **record}) + "\n")
    sys.stderr.flush()


def held_outside_processes(used: int, procs: list[tuple[int, int, int, str]]) -> int:
    """Memory in use that no process holds: tmpfs nobody maps, cache the stat did not cover."""
    return used - sum(rss for _pid, _uid, rss, _cmd in procs)


def no_kill_can_help(outside: int, limit: int) -> bool:
    """True when stopping even the smallest candidate would leave the container at its limit."""
    return outside + MIN_VICTIM_BYTES >= limit


class Guard:
    """The decision loop's state: the last kill until its process exits, and whether it is holding off."""

    def __init__(self, environ: dict[str, str], cgroup_root: str = CGROUP_ROOT, proc_root: str = PROC_ROOT):
        self.environ, self.cgroup_root, self.proc_root = environ, cgroup_root, proc_root
        # Processes sent SIGKILL that have not yet exited, and when: their memory is on its
        # way out (until SETTLE_SECONDS), and choosing one again frees nothing.
        self.signalled: dict[int, float] = {}
        self.holding = False
        self.no_candidate_logged = False
        self.gvisor = on_gvisor()
        self.readable: bool | None = None

    def tick(self, now: float) -> None:
        used = memory_in_use(self.cgroup_root)
        limit = memory_limit(self.environ, self.cgroup_root, self.proc_root)
        readable = used is not None and limit is not None
        if readable != self.readable:
            log({"event": "started" if readable else "disabled", "limit": limit, "used": used,
                 "high_watermark": high_watermark(limit) if limit else None})
            self.readable = readable
        if not readable:
            return
        high = effective_watermark(limit, self.gvisor, process_count(self.proc_root) if self.gvisor else 0)
        if used < high:
            self.signalled.clear()  # the episode is over; a later process may reuse a pid
            self.no_candidate_logged = False
            if self.holding:
                self.holding = False
                log({"event": "released", "used": used, "limit": limit})
            return
        procs = processes(self.proc_root)
        alive = {pid: rss for pid, _uid, rss, _cmd in procs}
        self.signalled = {pid: at for pid, at in self.signalled.items() if pid in alive}
        leaving = sum(alive[pid] for pid, at in self.signalled.items() if now - at < SETTLE_SECONDS)
        outside = held_outside_processes(used, procs)
        if no_kill_can_help(outside, limit):
            if not self.holding:
                self.holding = True
                log({"event": "holding", "reason": "memory is held outside processes", "used": used,
                     "outside": outside, "limit": limit})
            return
        if self.holding:
            self.holding = False
            log({"event": "released", "used": used, "outside": outside, "limit": limit})
        excess = used - (high - BATCH_MARGIN_BYTES) - leaving
        if excess <= 0:
            return  # what is already being freed covers it
        victims = choose_victims([p for p in procs if p[0] not in self.signalled], excess)
        if not victims:
            if not self.no_candidate_logged:
                self.no_candidate_logged = True
                largest = max((rss for _pid, uid, rss, _cmd in procs if uid != ROOT_UID), default=0)
                log({"event": "no-candidate", "reason": "no session process is large enough to stop",
                     "used": used, "limit": limit, "largest": largest, "floor": MIN_VICTIM_BYTES})
            return
        for pid, rss, cmd in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            self.signalled[pid] = now
            log({"event": "killed", "pid": pid, "rss": rss, "used": used, "limit": limit,
                 "high_watermark": high, "batch": len(victims), "command": loggable_command(cmd)})


def run(environ: dict[str, str], cgroup_root: str = CGROUP_ROOT, proc_root: str = PROC_ROOT) -> None:
    guard = Guard(environ, cgroup_root, proc_root)
    while True:
        try:
            guard.tick(time.monotonic())
        except Exception as exc:  # noqa: BLE001 -- one bad reading must not end the guard
            log({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    run(dict(os.environ))
