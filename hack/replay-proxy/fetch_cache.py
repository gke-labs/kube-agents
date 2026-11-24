"""Download what recent trusted nightlies selected from the shared replay cache, for the replay lane.

The nightly uploads answers from graded runs and, per call, a record of
the objects it confirmed in the bucket, with each one's size and, for a slot,
the answer it points at (publish_cache.py). The lane replays those nights: this
lists the records under gs://<bucket>/replay-cache/builds/ younger than
`--max-age-hours`, takes the objects they confirmed under this checkout's
normalizer version (a different version's keys never match), and downloads
them into a cache directory laid out the way replay_cache.py reads it
(variants nested again, one directory per context). It also writes the
records' runs, per build, for the lane's verdict comparison (lane_report.py),
from records back to the bucket's 30-day lifecycle: an answer's object, and
so its `served_from`, is the night that first wrote it, which can be that old,
though a night inside the window produced it; its verdict is read from that
night's newest record (one small object per older build). With
`--night-out` it writes the newest night's builds by the commit they name (the
night --print-commit chooses from, below), so the report can grade each case
against the night that ran the commit the lane replays (lane_report.py).

An answer is loaded only when a build in the window says its graded runs
produced it: an alternate (`alts/`) must be in an in-window record's
`produced` list or `produced_counts` for its build, so a record that merely
found an older night's object in the bucket never brings it in, and no answer
served is older than the window. A build lists an alternate under `produced`
only in the record of the call that first confirmed it, and counts it again in
every later record, so a build whose first record has aged out still makes
its alternate eligible through a newer record's count; the object is then
planned at no size and costed for real once downloaded. An alternate every
producing build in the window now counts 0 times (a producer of it was later
found to disagree with its tag) is refused and counted (`skipped`), even when
an earlier pass uploaded it. An older-layout answer (`answers/`, one per
key, from records written before alternates) is still taken as before, and
can be as old as the bucket's lifecycle.

For each key two or more alternates arrived for, it writes
`weights/<fingerprint>.json` (replay_cache.WEIGHTS_DIR): per answer, how many
requests from graded runs got it, which the proxy draws the key's answers in
proportion to. A build's records count every answer it put up again on each
call (`produced_counts`), so a build's count is its newest in-window record's,
never a sum of its records; builds are different runs, so their counts add.
Only builds whose in-window records list the answer (under `produced` or
`produced_counts`) count, so the weight covers the same window as the answers.
A build with no count for an answer it produced (a record written before
counts) adds 1; one whose newest count is 0 adds nothing. A weights file
past `--max-bytes` is left out and counted; its key's answers then weigh 1.

What fits under `--max-bytes` is chosen before anything downloads, in this
order, newest record first within each kind: answers in the older layout;
alternates; a weights file for each key with two or more chosen alternates;
slots whose key has a chosen answer; variants; messages. Weights come
straight after the answers they weigh, so a variant or message never takes
their room; the counts come from the records, so a file is charged at its
size over every chosen alternate, which is never less than the file written
over the ones that arrive. An object is costed as the disk it takes, its size
rounded up to whole 4 KiB blocks plus one block for its directory entry (and
a block for each new variant or alternate directory), from the size the
record gives. What does not fit is left out and counted, never refused: the
lane replays less rather than nothing. After the download each file is costed
again at its real size in the same order, the weights files written once the
alternates are in (an object the nightly found already in the bucket can
differ from the copy it recorded), and one past the bound is removed and
counted, so a weights file is left out only when the answers leave no room
for it. A slot whose
key has no answer that arrived (left out, expired, or never written) is
removed too, so no pointer names a key the pod has no answer for.

Only the nightly runner can create objects in the bucket (docs/ci-pool-projects.md,
"The replay cache bucket"). Object keys are still checked before anything
becomes a local path: every listed key must be a record at
`builds/<build>/publish-<ms>.json`, and every name a record confirms must be in
the cache layout (publish_cache.OBJECT_NAME_RE). A key or name that is not,
including one with an empty, `.` or `..` segment, refuses the whole download,
as the shadow bucket's recipe does (hack/replay-proxy/README.md, "In CI"): a
key outside the layout means the writer is not the code this reads.

Prints one JSON summary line; exits 1 when no answer arrived, a key was
refused, or the records could not be listed (`error: list_failed_<class>`, the
class as collect_shadow.error_class names it, never gcloud's own text), so the
lane stops before it spends a cluster on nothing. A bucket with no records
lists empty (`records: 0`), not as a failure.

`--print-commit` downloads nothing but records, and prints the commit the
newest recording night ran (each record's `job.commit`, publish_cache.py), so
the lane's Prow job can check it out before anything else runs and replay the
code that recorded. It lists as a fetch does and takes the same records: young
enough to fetch, of the lane's mode and forge, at any normalizer version
(main's can differ from the recording's, which is the point). A night is one
run of the nightly schedule: its builds all start together from the same main
and publish over the next hours. A build's start is the creation time of its
first record, and the newest night is every such build that started within
NIGHT_SPAN_HOURS of the newest one. A build's records all name its job, so it
reads the newest record of each build; only when the night's builds name more
than one commit does it read all their records, and print the commit whose
records confirmed answers to the most keys (a key's several alternates
counting once), saying so on stderr. Any listed key
outside the layout, and any record it reads that names an object outside the
layout or a commit that is not one, refuses as in a fetch. It prints nothing
and exits 1 when no build of that night names a commit (records written
before publish_cache.py wrote one), when it refuses, or when the listing
fails; the caller then stays on main. Its counts go to stderr, so stdout is
the commit or nothing.
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import collect_shadow
import normalizer
import publish_cache as P
import replay_cache

CACHE_ROOT = P.CACHE_ROOT
BUILDS_DIR = P.BUILDS_DIR
RUNS_FILE = "nightly-runs.json"
# --night-out's file and its fields: the newest night's builds, and those by the commit they name.
NIGHT_FILE = "newest-night.json"
NIGHT_BUILDS = "builds"
NIGHT_COMMIT_BUILDS = "commit_builds"
HEX = P.HEX
# A record's key under replay-cache/.
RECORD_KEY_RE = re.compile(rf"^builds/({P.BUILD_PATTERN})/publish-[0-9]{{1,16}}\.json\Z")
SAFE_HEX = P.SAFE_HEX
# One `gcloud storage ls -l` object line: size, creation time, URL.
LS_LINE_RE = re.compile(r"^\s*(\d+)\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z\s+(gs://\S+)\s*$")
LS_TOTAL_PREFIX = "TOTAL:"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
SECONDS_PER_HOUR = 3600
DEFAULT_MAX_AGE_HOURS = 30.0
# How far before the newest build's start a build may have started and still
# be the same night (--print-commit). A build's start is its first record, and
# a night's shards all start from the 00:00 UTC schedule: their first records
# land within the first hour or so (00:27-00:47 UTC on 2026-11-11 and -12),
# a shard that waited for a pool project somewhat later. Four hours covers that
# and leaves out a nightly rerun from Deck later in the day, which records too
# and would otherwise compete for the night with a day-old commit.
NIGHT_SPAN_HOURS = 4.0
# --print-commit's staging: each build's newest record, then a contested night's records.
NEWEST_RECORDS_DIR = "newest"
NIGHT_RECORDS_DIR = "night"
# The bucket's lifecycle rule deletes an object this long after it was written
# (docs/ci-pool-projects.md, "The replay cache bucket"), so an answer the lane
# serves can have been first written by a night this old. Runs, for the verdict
# comparison, are read from records back this far; objects only from the
# --max-age-hours window.
CACHE_LIFECYCLE_DAYS = 30
SECONDS_PER_DAY = 86400
# `gcloud storage ls` on a prefix that matches nothing exits non-zero with this:
# an empty bucket, not a failure.
LS_MATCHED_NOTHING_RE = re.compile(r"matched no objects", re.IGNORECASE)
LIST_FAILED = "list_failed"
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
# The cost of an object on the pod's disk: whole blocks, and a block for its
# directory entry (spool.py counts blocks for the same reason).
DISK_BLOCK_BYTES = 4096
FILE_OVERHEAD_BYTES = DISK_BLOCK_BYTES
GCLOUD_TIMEOUT_SECONDS = 600.0
TEXT_ENCODING = "utf-8"
STAGING_PREFIX = "replay-cache-fetch-"
GCLOUD_ENV = {"CLOUDSDK_CORE_DISABLE_PROMPTS": "1"}
# The order kinds are chosen in, and the kinds' names in the summary; the
# weights files (WEIGHTS) are charged between alternates and slots.
PRIORITY = (P.ANSWERS, P.ALTS, P.SLOTS, P.VARIANTS, P.MESSAGES)
# Kinds stored one directory deeper on disk (object_path), each new directory a block.
NESTED_KINDS = (P.VARIANTS, P.ALTS)
# An alternate a recent record confirms but no build in the window says it produced.
NOT_PRODUCED = "not_produced"
# An alternate every build in the window that produced it now counts 0 times
# (a producer later found disagreeing with its tag: publish_cache.py), so no
# graded run vouches for it any more.
ZERO_COUNT = "zero_count"
# The size an alternate is planned at when no in-window record confirmed it
# (its build's confirming record aged out): costed for real once downloaded.
UNKNOWN_SIZE = 0
# The weights files written beside the alternates (replay_cache.WEIGHTS_DIR), by name in the summary.
WEIGHTS = replay_cache.WEIGHTS_DIR

Run = Callable[..., Optional[subprocess.CompletedProcess]]


@dataclass
class NightCommit:
    """--print-commit's answer: the newest night's commit, and what it was chosen from."""

    commit: str = ""
    records: int = 0
    other_job_records: int = 0
    builds: List[str] = field(default_factory=list)
    # The night's builds that name no commit, and the others by the commit they name.
    uncommitted_builds: int = 0
    commit_builds: Dict[str, List[str]] = field(default_factory=dict)
    # Per commit, the keys its records confirmed an answer to: counted only when the night names more than one.
    answers: Dict[str, int] = field(default_factory=dict)


class Refused(Exception):
    """A key or name outside the layout: the download stops whole."""


class ListFailed(Exception):
    """The records could not be listed; `args[0]` is the failure's class, never gcloud's own text."""


@dataclass
class Fetched:
    records: int = 0
    # Older records read only for their runs (CACHE_LIFECYCLE_DAYS).
    verdict_records: int = 0
    builds: List[str] = field(default_factory=list)
    objects: int = 0
    # Recent records whose job's mode or forge isn't the lane's, or that name none.
    other_job_records: int = 0
    downloaded: int = 0
    bytes: int = 0
    by_kind: Dict[str, int] = field(default_factory=dict)
    left_out: Dict[str, int] = field(default_factory=dict)
    removed: Dict[str, int] = field(default_factory=dict)
    # Alternates the lane may not load (NOT_PRODUCED, ZERO_COUNT).
    skipped: Dict[str, int] = field(default_factory=dict)
    runs: Dict[str, List[Dict[str, object]]] = field(default_factory=dict)
    # The newest night of the lane's mode and forge (newest_night), as --night-out writes it.
    night_builds: List[str] = field(default_factory=list)
    night_commit_builds: Dict[str, List[str]] = field(default_factory=dict)

    def count(self, table: Dict[str, int], kind: str, n: int = 1) -> None:
        table[kind] = table.get(kind, 0) + n


def disk_cost(size: int) -> int:
    """What a file of `size` bytes takes on disk: whole blocks plus its directory entry."""
    return -(-max(size, 0) // DISK_BLOCK_BYTES) * DISK_BLOCK_BYTES + FILE_OVERHEAD_BYTES


def parse_listing(text: str, bucket: str) -> List[Tuple[int, float, str]]:
    """(size, created epoch, key under replay-cache/) of every listed object; Refused on anything else."""
    prefix = f"gs://{bucket}/{CACHE_ROOT}/"
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(LS_TOTAL_PREFIX):
            continue
        match = LS_LINE_RE.match(raw)
        if not match or not match.group(3).startswith(prefix):
            raise Refused("a listed line is not an object of the cache bucket")
        created = calendar.timegm(time.strptime(match.group(2), TIME_FORMAT))
        out.append((int(match.group(1)), float(created), match.group(3)[len(prefix):]))
    return out


def nested_dir(name: str) -> str:
    """The directory a variant or alternate is nested in on disk (its context or key), or empty for any other kind."""
    sub, _, base = name.partition("/")
    return f"{sub}/{base.partition(P.VARIANT_SEPARATOR)[0]}" if sub in NESTED_KINDS else ""


def object_path(dest: str, name: str) -> str:
    """Where replay_cache.py reads an object: variants and alternates nested one directory per context or key."""
    sub, _, base = name.partition("/")
    if sub in NESTED_KINDS:
        context, _, answer = base.partition(P.VARIANT_SEPARATOR)
        return os.path.join(dest, sub, context, answer)
    return os.path.join(dest, sub, base)


def newest_night(started: Dict[str, float], commits: Dict[str, str]) -> Tuple[List[str], Dict[str, List[str]], int]:
    """The newest night among `commits`' builds (module docstring, --print-commit).

    `started` is each build's first record's creation time; `commits` the
    commit each build of the lane's mode and forge names, empty for none.
    Returns the night's builds, those by the commit they name, and how many name none.
    """
    if not commits:
        return [], {}, 0
    newest = max(started[build] for build in commits)
    builds = sorted(b for b in commits if started[b] >= newest - NIGHT_SPAN_HOURS * SECONDS_PER_HOUR)
    commit_builds: Dict[str, List[str]] = {}
    for build in builds:
        if commits[build]:
            commit_builds.setdefault(commits[build], []).append(build)
    return builds, commit_builds, sum(1 for b in builds if not commits[b])


class Fetcher:
    def __init__(self, *, bucket: str, version: str, run: Run, max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                 max_bytes: int = DEFAULT_MAX_BYTES, clock: Callable[[], float] = time.time,
                 mode: str = P.MODE_NEXT, forge: str = P.DEFAULT_FORGE):
        self.bucket = bucket
        # The lane's own install mode and forge: objects come only from records of the same.
        self.mode = mode
        self.forge = forge
        self.version = version
        self.run = run
        self.max_age_seconds = max_age_hours * SECONDS_PER_HOUR
        self.verdict_age_seconds = max(CACHE_LIFECYCLE_DAYS * SECONDS_PER_DAY, self.max_age_seconds)
        self.max_bytes = max_bytes
        self.clock = clock

    def _gcloud(self, kind: str, args: Sequence[str], input: Optional[bytes] = None) -> Optional[subprocess.CompletedProcess]:
        return self.run(kind, ["gcloud", "storage", *args], GCLOUD_TIMEOUT_SECONDS, input=input, env=GCLOUD_ENV)

    def listed(self) -> List[Tuple[float, str]]:
        """(created, key) of every publish record in the bucket, oldest first.

        An empty bucket lists nothing; a listing that fails raises ListFailed
        with the failure's class, so a lane that can't read the bucket never
        reads as one with nothing to replay.
        """
        proc = self._gcloud("list_records", ["ls", "-l", f"gs://{self.bucket}/{CACHE_ROOT}/{BUILDS_DIR}/**"])
        if proc is None:
            raise ListFailed(LIST_FAILED)
        if proc.returncode != 0:
            if LS_MATCHED_NOTHING_RE.search((proc.stderr or b"").decode(TEXT_ENCODING, "replace")):
                return []
            raise ListFailed(f"{LIST_FAILED}_{collect_shadow.error_class(proc.stderr)}")
        listed = parse_listing(proc.stdout.decode(TEXT_ENCODING, "replace"), self.bucket)
        for _, _, key in listed:
            if not RECORD_KEY_RE.match(key):
                raise Refused("a key under builds/ is not a publish record")
        return [(created, key) for _, created, key in sorted(listed, key=lambda item: (item[1], item[2]))]

    def records(self) -> List[str]:
        """Keys of the records young enough to replay, oldest first."""
        oldest = self.clock() - self.max_age_seconds
        return [key for created, key in self.listed() if created >= oldest]

    def _download(self, kind: str, urls: Sequence[str], into: str) -> None:
        os.makedirs(into, exist_ok=True)
        # --continue-on-error: an object a lifecycle rule has expired since its
        # record was written is skipped, not the end of the download.
        self._gcloud(kind, ["cp", "--continue-on-error", "-I", into + os.sep],
                     input=("\n".join(urls) + "\n").encode())

    def _confirmed(self, record: Dict[str, object], version: Optional[str] = None) -> List[Tuple[str, int, str]]:
        """(name, bytes, slot target) of every object a record confirms at a version (default this one); Refused on any outside the layout."""
        out = []
        objects = (record.get(P.RECORD_OBJECTS) or {}) if isinstance(record.get(P.RECORD_OBJECTS), dict) else {}
        for item in objects.get(self.version if version is None else version) or []:
            name = item.get(P.OBJECT_NAME) if isinstance(item, dict) else None
            size = item.get(P.OBJECT_BYTES) if isinstance(item, dict) else None
            if not isinstance(name, str) or not P.OBJECT_NAME_RE.match(name):
                raise Refused("a record confirms a name outside the cache layout")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise Refused("a record gives an object no size")
            target = str(item.get(P.OBJECT_TARGET) or "")
            if name.startswith(P.SLOTS + "/") and not SAFE_HEX.match(target):
                raise Refused("a record gives a slot no answer")
            out.append((name, size, target))
        return out

    def _produced(self, record: Dict[str, object]) -> Set[Tuple[str, str]]:
        """(fingerprint, answer) of every alternate a record says its night's graded runs produced, at this version.

        Refused when the field is there but not in its shape: the writer is
        then not the code this reads.
        """
        produced = record.get(P.RECORD_PRODUCED)
        if produced is None:
            return set()
        if not isinstance(produced, dict):
            raise Refused("a record's produced answers are not a map")
        out: Set[Tuple[str, str]] = set()
        for version, keys in produced.items():
            if not isinstance(keys, dict):
                raise Refused("a record's produced answers are not a map")
            for fingerprint, answers in keys.items():
                if not SAFE_HEX.match(str(fingerprint)) or not isinstance(answers, list) or not all(
                        isinstance(a, str) and SAFE_HEX.match(a) for a in answers):
                    raise Refused("a record names a produced answer outside the cache layout")
                if version == self.version:
                    out.update((fingerprint, answer) for answer in answers)
        return out

    def _produced_counts(self, record: Dict[str, object]) -> Dict[Tuple[str, str], int]:
        """(fingerprint, answer) -> count of every alternate a record counts (RECORD_PRODUCED_COUNTS), at this version.

        Empty for a record written before the field. Refused when it is there
        but not in its shape, as _produced is.
        """
        counts = record.get(P.RECORD_PRODUCED_COUNTS)
        if counts is None:
            return {}
        if not isinstance(counts, dict):
            raise Refused("a record's produced counts are not a map")
        out: Dict[Tuple[str, str], int] = {}
        for version, keys in counts.items():
            if not isinstance(keys, dict):
                raise Refused("a record's produced counts are not a map")
            for fingerprint, answers in keys.items():
                if not SAFE_HEX.match(str(fingerprint)) or not isinstance(answers, dict):
                    raise Refused("a record counts an answer outside the cache layout")
                for answer, count in answers.items():
                    if not (isinstance(answer, str) and SAFE_HEX.match(answer)) or not isinstance(count, int) \
                            or isinstance(count, bool) or count < 0:
                        raise Refused("a record counts an answer outside the cache layout")
                    if version == self.version:
                        out[(fingerprint, answer)] = count
        return out

    def _same_job(self, record: Dict[str, object]) -> bool:
        """Whether a record's job graded the lane's own mode on its forge; a record naming no job doesn't."""
        job = record.get(P.RECORD_JOB)
        return isinstance(job, dict) and job.get(P.JOB_MODE) == self.mode and job.get(P.JOB_FORGE) == self.forge

    def _commit(self, record: Dict[str, object]) -> str:
        """The commit a record's job names, or empty for none; Refused on one that is not a commit."""
        job = record.get(P.RECORD_JOB)
        commit = job.get(P.JOB_COMMIT, "") if isinstance(job, dict) else ""
        if commit and not (isinstance(commit, str) and P.COMMIT_RE.match(commit)):
            raise Refused("a record names a commit that is not one")
        return commit or ""

    def _read_records(self, keys: Sequence[str], stage: str) -> Dict[str, object]:
        """Each record's content by key, downloaded in as few copies as their names allow.

        A copy lands every object in one directory by its base name, which is
        unique within a build only, so keys whose names collide go in later copies.
        """
        out: Dict[str, object] = {}
        pending = list(keys)
        copy = 0
        while pending:
            batch: List[str] = []
            names: Set[str] = set()
            rest: List[str] = []
            for key in pending:
                name = os.path.basename(key)
                if name in names:
                    rest.append(key)
                else:
                    names.add(name)
                    batch.append(key)
            into = os.path.join(stage, str(copy))
            self._download("fetch_records", [f"gs://{self.bucket}/{CACHE_ROOT}/{k}" for k in batch], into)
            for key in batch:
                out[key] = P.read_json(os.path.join(into, os.path.basename(key)))
            pending, copy = rest, copy + 1
        return out

    def _checked(self, record: Dict[str, object]) -> str:
        """A record's commit, after the checks a fetch makes of every version's names (Refused on any)."""
        objects = record.get(P.RECORD_OBJECTS)
        for version in (objects if isinstance(objects, dict) else {}):
            self._confirmed(record, version)
        self._produced(record)
        self._produced_counts(record)
        return self._commit(record)

    def night_commit(self) -> NightCommit:
        """The commit the newest night of the lane's mode and forge ran (module docstring, --print-commit).

        A build's records all carry its job, so its newest record says its
        mode, forge and commit: one small object per build. Only when the
        night's builds name more than one commit are all their records read,
        to count the keys (version, fingerprint) each commit's records
        confirmed an answer to, whatever the number of alternates per key.
        """
        result = NightCommit()
        listed = self.listed()
        now = self.clock()
        started: Dict[str, float] = {}
        newest_key: Dict[str, str] = {}
        by_build: Dict[str, List[str]] = {}
        for created, key in listed:  # oldest first: a build's first record sets its start
            build = RECORD_KEY_RE.match(key).group(1)  # type: ignore[union-attr]
            started.setdefault(build, created)
            if created >= now - self.max_age_seconds:
                newest_key[build] = key
                by_build.setdefault(build, []).append(key)
        stage = tempfile.mkdtemp(prefix=STAGING_PREFIX)
        try:
            commits: Dict[str, str] = {}
            for key, record in self._read_records(sorted(newest_key.values()), os.path.join(stage, NEWEST_RECORDS_DIR)).items():
                if not isinstance(record, dict):
                    continue
                result.records += 1
                commit = self._checked(record)
                if not self._same_job(record):
                    result.other_job_records += 1
                    continue
                commits[RECORD_KEY_RE.match(key).group(1)] = commit  # type: ignore[union-attr]
            if not commits:
                return result
            result.builds, result.commit_builds, result.uncommitted_builds = newest_night(started, commits)
            if len(result.commit_builds) == 1:
                result.commit = next(iter(result.commit_builds))
            if len(result.commit_builds) <= 1:
                return result
            night_keys = [k for b in result.builds if commits[b] for k in by_build[b]]
            answers: Dict[str, Set[Tuple[str, str]]] = {c: set() for c in result.commit_builds}
            for key, record in self._read_records(night_keys, os.path.join(stage, NIGHT_RECORDS_DIR)).items():
                if not isinstance(record, dict):
                    continue
                self._checked(record)
                build = RECORD_KEY_RE.match(key).group(1)  # type: ignore[union-attr]
                objects = record.get(P.RECORD_OBJECTS)
                for version in (objects if isinstance(objects, dict) else {}):
                    # Requests covered, not objects: a key's several alternates count once.
                    answers[commits[build]].update((version, P.answered_key(name))
                                                   for name, _, _ in self._confirmed(record, version)
                                                   if P.answered_key(name) is not None)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        result.answers = {commit: len(names) for commit, names in answers.items()}
        latest = {c: max(started[b] for b in builds) for c, builds in result.commit_builds.items()}
        # The most keys answered; then the latest build to start; then by name, so the choice is stable.
        result.commit = max(answers, key=lambda c: (len(answers[c]), latest[c], c))
        return result

    def _plan(self, confirmed: List[Tuple[str, int, str]], result: Fetched,
              produced_by: Dict[str, Set[Tuple[str, str]]], counted: Dict[str, Dict[Tuple[str, str], int]],
              ) -> Tuple[Dict[str, List[Tuple[str, int, str]]], Set[str]]:
        """What fits under max_bytes, by kind in PRIORITY order with weights after alternates; the rest counted as left out.

        Returns the objects chosen by kind and the keys whose weights file is
        charged: each key with two or more chosen alternates, at the cost of
        its file over all of them, which is never less than the file written
        over the ones that arrive.
        """
        by_kind: Dict[str, List[Tuple[str, int, str]]] = {kind: [] for kind in PRIORITY}
        for item in confirmed:
            by_kind[item[0].partition("/")[0]].append(item)
        chosen: Dict[str, List[Tuple[str, int, str]]] = {kind: [] for kind in PRIORITY}
        answers: Set[str] = set()
        contexts: Set[str] = set()
        alternates_of: Dict[str, List[str]] = {}
        weighed: Set[str] = set()
        used = 0
        for kind in PRIORITY:
            for name, size, target in by_kind[kind]:
                if kind == P.SLOTS and target not in answers:
                    result.count(result.left_out, kind)
                    continue
                cost = disk_cost(size)
                context = nested_dir(name)
                if context and context not in contexts:
                    cost += DISK_BLOCK_BYTES
                if used + cost > self.max_bytes:
                    result.count(result.left_out, kind)
                    continue
                used += cost
                if context:
                    contexts.add(context)
                key = P.answered_key(name)
                if key is not None:
                    answers.add(key)
                parts = P.alternate_parts(name)
                if parts is not None:
                    alternates_of.setdefault(parts[0], []).append(parts[1])
                chosen[kind].append((name, size, target))
            if kind != P.ALTS:
                continue
            for fingerprint, names in sorted(alternates_of.items()):
                if len(names) < 2:
                    continue
                cost = weights_cost(weights_of(fingerprint, names, produced_by, counted))
                if used + cost > self.max_bytes:
                    result.count(result.left_out, WEIGHTS)
                    continue
                used += cost
                weighed.add(fingerprint)
        return chosen, weighed

    def _write_weights(self, dest: str, produced_by: Dict[str, Set[Tuple[str, str]]],
                       counted: Dict[str, Dict[Tuple[str, str], int]], answers_of: Dict[str, List[str]],
                       weighed: Set[str], used: int, result: Fetched) -> int:
        """A weights file (replay_cache.WEIGHTS_DIR) for every planned key two or more alternates arrived for, within the bound.

        A key with one answer has nothing to weigh. A file past the bound is
        left out and counted: its key's answers then each weigh 1. Returns
        the bytes used, these files included.
        """
        cache = replay_cache.AnswerCache(dest)
        for fingerprint, names in sorted(answers_of.items()):
            if len(names) < 2 or fingerprint not in weighed:
                continue
            weights = weights_of(fingerprint, names, produced_by, counted)
            cost = weights_cost(weights)
            if used + cost > self.max_bytes:
                result.count(result.left_out, WEIGHTS)
                continue
            if cache.save_weights(fingerprint, weights):
                used += cost
                result.count(result.by_kind, WEIGHTS)
        return used

    def fetch(self, dest: str, runs_out: Optional[str] = None, night_out: Optional[str] = None) -> Fetched:
        result = Fetched()
        listed = self.listed()
        now = self.clock()
        started: Dict[str, float] = {}
        for created, key in listed:  # oldest first: a build's first record sets its start
            started.setdefault(RECORD_KEY_RE.match(key).group(1), created)  # type: ignore[union-attr]
        commits: Dict[str, str] = {}
        keys = [key for created, key in listed if created >= now - self.max_age_seconds]
        # Older builds, within the lifecycle, for their runs only: the newest
        # record of each holds every run it graded (a later record never drops
        # one), so one small object per build, about three a night.
        recent_builds = {RECORD_KEY_RE.match(k).group(1) for k in keys}  # type: ignore[union-attr]
        verdict_keys: Dict[str, str] = {}
        for created, key in listed:
            build = RECORD_KEY_RE.match(key).group(1)  # type: ignore[union-attr]
            if build not in recent_builds and created >= now - self.verdict_age_seconds:
                verdict_keys[build] = key  # oldest first, so the newest wins
        stage = tempfile.mkdtemp(prefix=STAGING_PREFIX)
        try:
            by_build: Dict[str, List[str]] = {}
            for key in [*keys, *verdict_keys.values()]:
                by_build.setdefault(RECORD_KEY_RE.match(key).group(1), []).append(key)  # type: ignore[union-attr]
            # One copy per build: a record's name is unique within its build only.
            for build, build_keys in by_build.items():
                self._download("fetch_records", [f"gs://{self.bucket}/{CACHE_ROOT}/{k}" for k in build_keys],
                               os.path.join(stage, BUILDS_DIR, build))
            for build, key in verdict_keys.items():
                record = P.read_json(os.path.join(stage, BUILDS_DIR, build, os.path.basename(key)))
                runs = record.get(P.RECORD_RUNS) if isinstance(record, dict) else None
                if isinstance(runs, list):
                    result.verdict_records += 1
                    result.runs[build] = [r for r in runs if isinstance(r, dict)]
            confirmed: Dict[str, Tuple[str, int, str]] = {}
            # Per build, the alternates its in-window records say it produced
            # (listed or counted), and its newest record's count of each (weight()).
            produced_by: Dict[str, Set[Tuple[str, str]]] = {}
            counted: Dict[str, Dict[Tuple[str, str], int]] = {}
            # Every alternate an in-window record confirmed, decided once all are read.
            alternates: Dict[str, Tuple[str, int, str]] = {}
            # Newest first, so what the bound leaves out is the oldest.
            for key in reversed(keys):
                build = RECORD_KEY_RE.match(key).group(1)  # type: ignore[union-attr]
                record = P.read_json(os.path.join(stage, BUILDS_DIR, build, os.path.basename(key)))
                if not isinstance(record, dict):
                    continue
                result.records += 1
                if build not in result.builds:
                    result.builds.append(build)
                runs = record.get(P.RECORD_RUNS)
                if isinstance(runs, list) and len(runs) > len(result.runs.get(build, [])):
                    result.runs[build] = [r for r in runs if isinstance(r, dict)]
                # Every record's names are checked, matching or not: a name
                # outside the layout means the writer is not this code.
                items = self._confirmed(record)
                produced = self._produced(record)
                counts = self._produced_counts(record)
                if not self._same_job(record):
                    result.other_job_records += 1
                    continue
                produced_by.setdefault(build, set()).update(produced, counts)
                for pair, count in counts.items():
                    counted.setdefault(build, {}).setdefault(pair, count)  # newest record first
                try:
                    commits.setdefault(build, self._commit(record))
                except Refused:
                    # The cache is still fetched: this build counts as naming
                    # no commit in the night file, and the others keep theirs.
                    commits.setdefault(build, "")
                for item in items:
                    if P.alternate_parts(item[0]) is not None:
                        alternates.setdefault(item[0], item)
                    else:
                        confirmed.setdefault(item[0], item)
            eligible = set().union(*produced_by.values()) if produced_by else set()
            for name, item in alternates.items():
                pair = P.alternate_parts(name)
                if pair not in eligible:
                    # Confirmed in the bucket, but no build in the window produced
                    # it: it could be an older night's, so it never comes in.
                    result.count(result.skipped, NOT_PRODUCED)
                elif weight(pair, produced_by, counted) == 0:  # type: ignore[arg-type]
                    result.count(result.skipped, ZERO_COUNT)
                else:
                    confirmed.setdefault(name, item)
            for fingerprint, answer in sorted(eligible):
                name = f"{P.ALTS}/{fingerprint}{P.VARIANT_SEPARATOR}{answer}{P.JSON_SUFFIX}"
                if name in alternates:
                    continue
                # Counted by a build whose confirming record aged out of the window.
                if weight((fingerprint, answer), produced_by, counted) == 0:
                    result.count(result.skipped, ZERO_COUNT)
                else:
                    confirmed.setdefault(name, (name, UNKNOWN_SIZE, ""))
            result.objects = len(confirmed)
            chosen, weighed = self._plan(list(confirmed.values()), result, produced_by, counted)
            prefix = P.cache_prefix(self.bucket, self.version)
            used = 0
            contexts: Set[str] = set()
            answers: Set[str] = set()
            # Each key's alternates that arrived, for its weights file.
            answers_of: Dict[str, List[str]] = {}
            for kind in PRIORITY:
                if kind == P.SLOTS:
                    # Right after the alternates, before anything below them can take the room.
                    used = self._write_weights(dest, produced_by, counted, answers_of, weighed, used, result)
                if not chosen[kind]:
                    continue
                into = os.path.join(stage, kind)
                self._download("fetch_objects", [f"{prefix}/{name}" for name, _, _ in chosen[kind]], into)
                for name, _, target in chosen[kind]:
                    got = os.path.join(into, os.path.basename(name))
                    if not os.path.isfile(got) or os.path.islink(got):
                        continue
                    if kind == P.SLOTS and (P.read_json(got) != target or target not in answers):
                        result.count(result.removed, kind)
                        continue
                    size = os.path.getsize(got)
                    cost = disk_cost(size)
                    context = nested_dir(name)
                    if context and context not in contexts:
                        cost += DISK_BLOCK_BYTES
                    if used + cost > self.max_bytes:
                        result.count(result.removed, kind)
                        continue
                    used += cost
                    if context:
                        contexts.add(context)
                    target_path = object_path(dest, name)
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    shutil.move(got, target_path)
                    key = P.answered_key(name)
                    if key is not None:
                        answers.add(key)
                    parts = P.alternate_parts(name)
                    if parts is not None:
                        answers_of.setdefault(parts[0], []).append(parts[1])
                    result.downloaded += 1
                    result.bytes += size
                    result.count(result.by_kind, kind)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        result.night_builds, result.night_commit_builds, _ = newest_night(started, commits)
        if runs_out:
            with open(runs_out, "w", encoding=TEXT_ENCODING) as handle:
                json.dump(result.runs, handle, sort_keys=True)
        if night_out:
            with open(night_out, "w", encoding=TEXT_ENCODING) as handle:
                json.dump({NIGHT_BUILDS: result.night_builds, NIGHT_COMMIT_BUILDS: result.night_commit_builds},
                          handle, sort_keys=True)
        return result


def weight(pair: Tuple[str, str], produced_by: Dict[str, Set[Tuple[str, str]]],
           counted: Dict[str, Dict[Tuple[str, str], int]]) -> int:
    """How often the window's graded runs produced one alternate: summed over the builds that produced it.

    A build's records count again on every call (publish_cache.RECORD_PRODUCED_COUNTS),
    so its newest record's count is the build's and its records are not
    summed; different builds are different runs, so they are. A build whose
    records give no count (written before them) adds 1: it published the
    answer, so it produced it at least once. A build whose newest count is 0
    adds nothing: an answer it put up on an earlier pass lost its graded
    producer. 0 overall means no build in the window vouches for the answer,
    and fetch refuses it.
    """
    return sum(counted.get(build, {}).get(pair, 1)
               for build, pairs in sorted(produced_by.items()) if pair in pairs)


def weights_of(fingerprint: str, names: Sequence[str], produced_by: Dict[str, Set[Tuple[str, str]]],
               counted: Dict[str, Dict[Tuple[str, str], int]]) -> Dict[str, int]:
    """A key's weights file content: each named alternate's weight()."""
    return {name: weight((fingerprint, name), produced_by, counted) for name in sorted(names)}


def weights_cost(weights: Dict[str, int]) -> int:
    """What a weights file takes on disk, written as replay_cache.AnswerCache.save_weights writes it."""
    return disk_cost(len(json.dumps(weights, sort_keys=True).encode(TEXT_ENCODING)))


def answers_arrived(dest: str) -> bool:
    """Whether any answer, in either layout, is in the filled cache directory."""
    for sub in (P.ANSWERS, P.ALTS):
        try:
            if os.listdir(os.path.join(dest, sub)):
                return True
        except OSError:
            continue
    return False


def default_run(kind: str, cmd: Sequence[str], timeout: float, input: Optional[bytes] = None,
                env: Optional[Dict[str, str]] = None) -> Optional[subprocess.CompletedProcess]:
    merged = dict(os.environ)
    merged.update(env or {})
    try:
        proc = subprocess.run(list(cmd), input=input, capture_output=True, timeout=timeout, env=merged,
                              stdin=None if input is not None else subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return None
    # `cp --continue-on-error` exits non-zero when it skipped an object, and
    # `ls` when it matched nothing; the caller reads both from the status and
    # stderr (Fetcher.listed tells an empty bucket from a failed listing).
    if proc.returncode != 0 and kind not in ("fetch_records", "fetch_objects", "list_records"):
        return None
    return proc


def print_commit(fetcher: Fetcher) -> int:
    """--print-commit: the newest night's commit on stdout and 0, or nothing on stdout and 1; counts on stderr."""
    summary: Dict[str, object] = {"event": "print_commit"}
    try:
        night = fetcher.night_commit()
    except Refused as exc:
        summary.update(error="refused", reason=str(exc))
        print(json.dumps(summary, sort_keys=True), file=sys.stderr)
        return 1
    except ListFailed as exc:
        summary["error"] = str(exc)
        print(json.dumps(summary, sort_keys=True), file=sys.stderr)
        return 1
    summary.update(records=night.records, other_job_records=night.other_job_records, builds=night.builds,
                   uncommitted_builds=night.uncommitted_builds, commit_builds=night.commit_builds,
                   answers=night.answers)
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    if not night.commit:
        print("no record of the newest night names a commit; the lane stays on main", file=sys.stderr)
        return 1
    if len(night.answers) > 1:
        print(f"the newest night's records name {len(night.answers)} commits; printing {night.commit},"
              f" whose records confirmed the most answers ({night.answers[night.commit]})", file=sys.stderr)
    print(night.commit)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--dest", default="", help="the cache directory to fill (replay_cache.py's layout)")
    parser.add_argument("--print-commit", action="store_true",
                        help="print the commit the newest recording night ran, and download nothing else")
    parser.add_argument("--runs-out", default="", help=f"where to write the records' runs per build ({RUNS_FILE})")
    parser.add_argument("--night-out", default="",
                        help=f"where to write the newest night's builds by the commit they name ({NIGHT_FILE})")
    parser.add_argument("--version", default=normalizer.NORMALIZER_VERSION,
                        help="the normalizer version to fetch (default: this checkout's)")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--forge", default="", help="the lane's EVAL_FORGE (empty is github)")
    args = parser.parse_args(argv)
    if not args.print_commit and not args.dest:
        parser.error("--dest is required unless --print-commit")
    summary: Dict[str, object] = {"event": "fetch", "version": args.version}
    if not P.valid_bucket(args.bucket) or not P.NORMALIZER_VERSION_RE.match(args.version):
        summary["error"] = "bad_arguments"
        # --print-commit keeps stdout for the commit alone.
        print(json.dumps(summary, sort_keys=True), file=sys.stderr if args.print_commit else sys.stdout)
        return 1
    job = P.job_identity("", args.forge)
    fetcher = Fetcher(bucket=args.bucket, version=args.version, run=default_run,
                      max_age_hours=args.max_age_hours, max_bytes=args.max_bytes,
                      mode=job[P.JOB_MODE], forge=job[P.JOB_FORGE])
    if args.print_commit:
        return print_commit(fetcher)
    try:
        result = fetcher.fetch(args.dest, args.runs_out or None, args.night_out or None)
    except Refused as exc:
        summary["error"] = "refused"
        summary["reason"] = str(exc)
        print(json.dumps(summary, sort_keys=True))
        return 1
    except ListFailed as exc:
        # The class only (collect_shadow.error_class): gcloud's own text can name the bucket.
        summary["error"] = str(exc)
        print(json.dumps(summary, sort_keys=True))
        return 1
    summary.update(records=result.records, verdict_records=result.verdict_records,
                   other_job_records=result.other_job_records, builds=len(result.builds),
                   objects=result.objects,
                   downloaded=result.downloaded, bytes=result.bytes, fetched=result.by_kind,
                   left_out=result.left_out, removed=result.removed, skipped=result.skipped)
    print(json.dumps(summary, sort_keys=True))
    return 0 if answers_arrived(args.dest) else 1


if __name__ == "__main__":
    sys.exit(main())
