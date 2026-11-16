#!/usr/bin/env python3
"""report_status.py — the read side of the fleet-audit report store.

Projects `reports/<audit-id>/<owner>/<name>/{latest.json, runs/}` and the
in-flight notes `start` leaves in the scratch directory into one small JSON
document: each stream's liveness and, per repository it publishes to, the last
run's outcome without its findings document and the run ring's filenames. Design of record: docs/designs/fleet-audit-report-store.md.

Two consumers, each pinning one property of this file:

- `scripts/fleet_audit_status_view.py` runs it off-pod by streaming this file
  into the pod on stdin (`kubectl exec -i … -- python3 -`), which keeps the
  view working against an image built before this file was. Streamed stdin has
  no `__file__`, so nothing here may reference one, import a sibling module,
  or reach outside the standard library.
- `fleet-audit-reports/scripts/report_query.py` imports the reading helpers
  below so the two do not grow two parsers of the same files. The module
  therefore does no work at import time: every entry point is a function and
  the CLI sits behind `__main__`.

`root_exists` and the per-stream `error` are the keys that make failure
legible: "I could not look" must never render as "nothing is wrong".
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Identical to audit_report.py's, and deliberately not imported from it: this
# file is streamed into a pod whose image may predate that module's copy.
REPORTS_DIR = os.environ.get("FLEET_AUDIT_REPORTS_DIR") or "/opt/data/fleet-audit/reports"
SCRATCH_DIR = os.environ.get("FLEET_AUDIT_SCRATCH_DIR") or "/opt/data/scratch"

# audit_report.INFLIGHT_TTL_SECONDS, duplicated for the same reason. An
# in-flight note this old no longer holds the stream — the next `start` takes
# it — so the two numbers must be the same number or this surface and the
# lease disagree about whether a run holds the stream. A test pins them.
INFLIGHT_TTL_S = 2 * 60 * 60

# audit_report.inflight_path_for's spelling: `<scratch>/inflight_<audit>.json`.
INFLIGHT_PREFIX = "inflight_"
INFLIGHT_SUFFIX = ".json"

# audit_report.REPORT_REPO_SEGMENT_RE, duplicated for the same reason: one
# segment of the `owner/name` a store directory is keyed on, never `.`/`..`.
REPO_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+\Z")

#: How a store directory below a host spells the `/` between path segments,
#: as audit_report.reports_dir_for writes it.
STORE_PATH_SEPARATOR = "%2F"

# The stored record of the newest run, and the ring of runs beside it.
LATEST_NAME = "latest.json"
#: GitHub's host, as a host-qualified name spells it; its store is the bare
#: slug's (audit_report.reports_dir_for).
GITHUB_HOST = "github.com"
RUNS_DIR = "runs"
# `audit_report.REPORT_STAMP_FORMAT`: a ring entry's name is its envelope's
# `finished_at` in UTC, in this form, so the two compare as strings.
RUN_STAMP_FORMAT = "%Y%m%dT%H%M%S.%fZ"

# Always present on a projected `latest`, null when the envelope lacks them, so
# a reader never has to tell an absent key from a null one. Everything else the
# envelope carries except the keys below rides along untouched
# (`_project_latest`), so a key added to the envelope later reaches a reader
# without an edit here.
LATEST_KEYS = (
    "audit_id",
    "repo",
    "finished_at",
    "status",
    "issue_number",
    "issue_url",
    "partial",
    "coverage_gaps",
    "prs_opened",
    "prs_closed",
    "silent_ok",
    # False when `finish` withheld the delta over a lost memory, so a reader
    # does not print `new`/`resolved` of zero as though nothing changed.
    "delta_known",
    "id_scheme",
    # audit_report.PHASE_TIMER_KEYS: seconds per phase of the run. Null when
    # the run had no input for a timer, for example `collect_s` on a run
    # without a collector manifest.
    "inspect_s",
    "publish_s",
    "collect_s",
)

# Keys the projection never carries. `document` is the whole findings document
# and runs to megabytes; `ledger_body` is the rendered issue body, which is
# `finish`'s memory and not a status; the three id lists are summarised as
# `new`/`resolved`/`current`, and the reader that wants the ids themselves
# reads the envelope through `load_latest` instead.
_NEVER_PROJECTED = frozenset(
    {"document", "ledger_document", "ledger_body", "new_ids", "resolved_ids", "current_ids"}
)


def reports_root(root: str | None = None) -> str:
    """The store root: argument, then environment, then the module default.

    The environment is re-read here rather than trusted from import time so a
    caller that sets `FLEET_AUDIT_REPORTS_DIR` after importing the module gets
    the root it set; patching `REPORTS_DIR` on the module works too.
    """
    return str(root or os.environ.get("FLEET_AUDIT_REPORTS_DIR") or REPORTS_DIR)


def scratch_root(root: str | None = None) -> str:
    """Where `start` leaves in-flight notes, resolved the way `reports_root` is."""
    return str(root or os.environ.get("FLEET_AUDIT_SCRATCH_DIR") or SCRATCH_DIR)


def stream_ids(root: str) -> list[str]:
    """Every stream directory under the root, sorted; [] when it is missing.

    A stream is a directory, so a stray temp file never reads as a stream. Any
    other OSError propagates — `project` turns "the root is there but
    unreadable" into `root_exists: false` rather than into an empty fleet.
    """
    if isinstance(root, BrokerReportStore):
        return root.stream_ids()
    try:
        with os.scandir(root) as entries:
            return sorted(entry.name for entry in entries if entry.is_dir())
    except FileNotFoundError:
        return []


def _subdirs(path: str) -> list[str]:
    with os.scandir(path) as entries:
        return sorted(entry.name for entry in entries if entry.is_dir())


def scan_repo_dirs(root: str, audit_id: str) -> tuple[list[str], list[str]]:
    """Every `owner/name` directory under the stream, spelled as on disk, and
    one failure line per owner directory that could not be listed.

    An owner is listed on its own, so one unreadable owner (a different uid
    or umask on the shared volume) costs only its own repositories, not its
    readable siblings'. The stream directory's own failure other than absence
    still propagates: then there is nothing to list at all.
    """
    if isinstance(root, BrokerReportStore):
        return root.repo_dirs(audit_id), []
    try:
        owners = _subdirs(os.path.join(root, audit_id))
    except FileNotFoundError:
        return [], []
    dirs: list[str] = []
    unreadable: list[str] = []
    for owner in owners:
        if not REPO_SEGMENT_RE.match(owner):
            continue
        if "." in owner:
            # A host, or a GitHub owner with a dot in it (`repo_ref` admits
            # `my.org/repo`). `_scan_host` tells them apart by layout.
            _scan_host(root, audit_id, owner, dirs, unreadable)
            continue
        try:
            names = _subdirs(os.path.join(root, audit_id, owner))
        except OSError as exc:
            unreadable.append(_failure(f"{owner}/", exc))
            continue
        dirs.extend(f"{owner}/{name}" for name in names if REPO_SEGMENT_RE.match(name))
    return dirs, unreadable


def _scan_host(
    root: str, audit_id: str, host: str, dirs: list[str], unreadable: list[str]
) -> None:
    """Every `host/path` store below one host directory.

    One level, as for a GitHub owner: each directory below the host is one
    repository's store, named for its path with every `/` spelled `%2F`. A
    name that does not decode to two or more valid segments is not a store.
    No walk, so nothing has to be guessed from a directory's children -- a
    project or group named `runs` is just a segment in a name.
    """
    try:
        names = _subdirs(os.path.join(root, audit_id, host))
    except OSError as exc:
        unreadable.append(_failure(f"{host}/", exc))
        return
    for name in names:
        parts = name.split(STORE_PATH_SEPARATOR)
        if len(parts) == 1:
            # No `%2F`: not a store below a host, which always spells a path of
            # two or more segments that way, but a repository directory below a
            # dotted GitHub owner -- the two-level `owner/name` store
            # `reports_dir_for` writes for `my.org/repo`. Read off the layout,
            # as `store_path` opens it, rather than off the dot in the name.
            if REPO_SEGMENT_RE.match(name) and name not in (os.curdir, os.pardir):
                dirs.append(f"{host}/{name}")
            continue
        if len(parts) >= 2 and all(
            REPO_SEGMENT_RE.match(part) and part not in (os.curdir, os.pardir) for part in parts
        ):
            dirs.append(f"{host}/{'/'.join(parts)}")


def store_path(root: str, audit_id: str, repo: str) -> str:
    """The directory one stream keeps for one repository. ValueError for a
    `repo` that is neither `owner/name` nor `host/path`, so an argument can
    never walk out of it. Lower-cased, and laid out, as
    audit_report.reports_dir_for writes it: GitHub's names are not
    case-sensitive, so neither is the store; a repository on another forge is
    one directory below its host, the path spelled with `%2F`; and GitHub named with its host
    shares the bare slug's directory."""
    return os.path.join(root, audit_id, *_store_segments(repo))


def _store_segments(repo: str) -> list[str]:
    """The two directory names below the stream that `store_path` opens for
    `repo`. ValueError as `store_path`."""
    segments = str(repo).lower().split("/")
    if len(segments) == 3 and segments[0] == GITHUB_HOST:
        segments = segments[1:]
    if len(segments) > 2 and "." not in segments[0]:
        segments = []
    if len(segments) < 2 or not all(
        REPO_SEGMENT_RE.match(part) and part not in (os.curdir, os.pardir) for part in segments
    ):
        raise ValueError(f"repository {repo!r} is not owner/name or host/path")
    if len(segments) > 2:
        segments = [segments[0], STORE_PATH_SEPARATOR.join(segments[1:])]
    return segments


def broker_store_key(repo: str) -> str:
    """The broker's store key for `repo`: the directories `store_path` opens,
    as one URL segment (`acme%2Ffleet`, `gitlab.com%2Fa%252Fb`).

    credential_proxy.report_store_key spells the same key, and a test holds
    the two equal. ValueError as `store_path`.
    """
    return urllib.parse.quote("/".join(_store_segments(repo)), safe="")


def store_key(root: str, audit_id: str, repo: str) -> str:
    """The name `scan_repo_dirs` lists for the store `store_path` opens.

    Every spelling `store_path` accepts for one repository -- bare `owner/name`,
    `github.com/owner/name`, any casing -- maps to one directory, and this is
    that directory's name as the scanner spells it. A reader comparing a typed
    name against the listing compares this, so the two spellings cannot
    disagree about whether the store is there. ValueError as `store_path`.
    """
    if isinstance(root, BrokerReportStore):
        return "/".join(_store_segments(repo)).replace(STORE_PATH_SEPARATOR, "/")
    rel = os.path.relpath(store_path(root, audit_id, repo), os.path.join(root, audit_id))
    return rel.replace(os.sep, "/").replace(STORE_PATH_SEPARATOR, "/")


def in_flight_ids(scratch: str) -> list[str]:
    """Every stream with an in-flight note, sorted; [] when there is none.

    A stream's first run has a note before it has a store directory, and a
    running first run must not read as "never ran". OSError other than
    absence propagates: a scratch directory that cannot be listed is a lease
    nobody could read, not a fleet with nothing in flight.
    """
    try:
        names = os.listdir(scratch)
    except FileNotFoundError:
        return []
    ids = (
        name[len(INFLIGHT_PREFIX) : -len(INFLIGHT_SUFFIX)]
        for name in names
        if name.startswith(INFLIGHT_PREFIX)
        and name.endswith(INFLIGHT_SUFFIX)
        and len(name) > len(INFLIGHT_PREFIX) + len(INFLIGHT_SUFFIX)
    )
    # The id is joined onto the store root as a directory, and the scratch
    # directory is shared with the worker: `inflight_...json` would name `..`
    # and list the root's parent as that stream's repositories. One segment
    # the store could have written, never `.` or `..`, as `store_path` holds
    # the repository to.
    return sorted(
        audit_id
        for audit_id in ids
        if REPO_SEGMENT_RE.match(audit_id) and audit_id not in (os.curdir, os.pardir)
    )


def read_json(path: str) -> object:
    """One JSON file, parsed. Raises OSError or ValueError; callers catch."""
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _read_object(path: str) -> dict | None:
    """A JSON object, None when the file is absent, ValueError when it is not
    an object. A file holding a list parses fine and is still corrupt."""
    try:
        value = read_json(path)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    return value


def _parse_object(data: bytes | None) -> dict | None:
    """`_read_object` for bytes the broker store answered: None for None."""
    if data is None:
        return None
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    return value


def _lease_epoch(value: object) -> float | None:
    """`started_at` as an epoch a clock can be compared with and a date made
    of, or None. A millisecond epoch, `1e400` (JSON's inf) or NaN is no
    timestamp at all -- `datetime` refuses each -- so such a note is read as
    one that does not parse, from its mtime, rather than taking every
    stream's projection down with it."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, ValueError, OSError):
        return None
    return float(value)


def in_flight_since(scratch: str, audit_id: str) -> float | None:
    """When the run holding this stream started, or None when none holds it.

    audit_report._in_flight_since, restated: a note that exists but does not
    parse — a `start` that created it a moment ago, or a `started_at` that is
    not a usable timestamp — counts from its mtime, because an unreadable note
    is a claim, not an absence. A note that can
    be neither read nor stat'd raises OSError: the lease could not be looked
    at, which the caller reports rather than reading as "nothing in flight".
    """
    path = os.path.join(scratch, f"{INFLIGHT_PREFIX}{audit_id}{INFLIGHT_SUFFIX}")
    try:
        note = read_json(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        note = None
    started = _lease_epoch(note.get("started_at") if isinstance(note, dict) else None)
    if started is not None:
        return started
    try:
        return os.stat(path).st_mtime
    except FileNotFoundError:
        return None


def load_latest(root: str, audit_id: str, repo: str) -> dict | None:
    """The raw, whole `latest.json`, `document` included.

    The projection strips `document`; report_query.py needs it, so this helper
    is the one that does not.
    """
    if isinstance(root, BrokerReportStore):
        return _parse_object(root.read_latest(audit_id, repo))
    return _read_object(os.path.join(store_path(root, audit_id, repo), LATEST_NAME))


class RingReadError(ValueError):
    """The ring fallback failed; `name` is the path under the store that did,
    so a reader names that file rather than the absent `latest.json`."""

    def __init__(self, name: str, exc: Exception):
        super().__init__(str(exc))
        self.name = name


def load_last(root: str, audit_id: str, repo: str) -> tuple[dict | None, bool]:
    """The last run the store kept, whole, and whether it came off the ring.

    `finish` deletes `latest.json` just before it changes the ledger and
    restores it only on a completed write, so a run that failed in between leaves the ring
    and no `latest.json`. The newest ring entry is then the last run the store
    has, and the flag says a later run may have changed the ledger unrecorded.
    A ring entry newer than a `latest.json` that is present wins the same way,
    flagged: the second of `write_report`'s two writes failed.
    A failure reading the ring raises RingReadError naming the file.
    """
    envelope, name = load_last_named(root, audit_id, repo)
    return envelope, name not in (None, LATEST_NAME)


def load_last_named(root: str, audit_id: str, repo: str) -> tuple[dict | None, str | None]:
    """`load_last`, with the file it read: `latest.json`, the ring entry it
    fell back to, or None when there was nothing. A caller that names the run
    takes the name from here rather than listing the ring again, since a
    `finish` landing between two listings would pair one entry's name with
    another's content."""
    latest = load_latest(root, audit_id, repo)
    if latest is not None:
        return _newer_ring_entry(root, audit_id, repo, latest) or (latest, LATEST_NAME)
    try:
        runs = list_runs(root, audit_id, repo)
    except OSError as exc:
        raise RingReadError("runs/", exc) from exc
    if not runs:
        return None, None
    try:
        return load_run(root, audit_id, repo, runs[-1]), runs[-1]
    except (OSError, ValueError) as exc:
        raise RingReadError(f"runs/{runs[-1]}", exc) from exc


def _newer_ring_entry(
    root: str, audit_id: str, repo: str, latest: dict
) -> tuple[dict, str] | None:
    """The newest ring entry and its name when it is newer than `latest.json`.

    `write_report` writes the ring entry first and `latest.json` second, and a
    held-open clean run keeps the old `latest.json` when the second write
    fails. The ring then holds a run the file does not, and a reader quoting
    the file would report the run before as the last one. None when the file
    is the newest run, its `finished_at` does not parse, or the ring cannot be
    read: the file is readable, so it still answers.
    """
    try:
        finished = datetime.fromisoformat(str(latest.get("finished_at")))
        if finished.tzinfo is None:
            return None
        stamp = finished.astimezone(timezone.utc).strftime(RUN_STAMP_FORMAT)
        runs = list_runs(root, audit_id, repo)
    except (OSError, ValueError, TypeError):
        return None
    if not runs or runs[-1] <= f"{stamp}.json":
        return None
    try:
        entry = load_run(root, audit_id, repo, runs[-1])
    except (OSError, ValueError):
        return None
    return (entry, runs[-1]) if entry is not None else None


def list_runs(root: str, audit_id: str, repo: str) -> list[str]:
    """Filenames in `runs/`, sorted ascending — which is time order, because
    the stamp is UTC. [] when the ring does not exist yet."""
    if isinstance(root, BrokerReportStore):
        return root.list_runs(audit_id, repo)
    try:
        names = os.listdir(os.path.join(store_path(root, audit_id, repo), RUNS_DIR))
    except FileNotFoundError:
        return []
    # The atomic write replaces from a `.tmp` file in the same directory, so a
    # read that lands mid-write must not report the temp file as a run.
    return sorted(name for name in names if name.endswith(".json"))


def load_run(root: str, audit_id: str, repo: str, name: str) -> dict | None:
    """One ring entry, whole. None when that stamp is not in the ring."""
    if isinstance(root, BrokerReportStore):
        return _parse_object(root.read_run(audit_id, repo, name))
    return _read_object(os.path.join(store_path(root, audit_id, repo), RUNS_DIR, name))


def liveness(
    started: float | None,
    latest: dict | None,
    now_epoch: float,
    ttl: float = INFLIGHT_TTL_S,
    error: str | None = None,
) -> str:
    """Which of five states this stream is in.

    `running` and `died` are the lease's own rule, not a second opinion: a note
    younger than the TTL holds the stream (the next `start` is refused), an
    older one does not (the next `start` takes it over). `died` is a run that
    started and never reached `finish`; nothing here can tell whether its
    session is still going, only that it no longer holds the stream.

    The lease is read before `error`: it is the stream's, so one repository's
    unreadable file must not hide a run holding, or dead on, every repository.
    The error still rides beside it in the projection's `error` key.
    """
    if started is not None:
        return "running" if now_epoch - started < ttl else "died"
    if error:
        return "error"
    if latest is not None:
        return "completed"
    return "never"


def project(
    root: str | None = None,
    now: float | datetime | None = None,
    scratch: str | None = None,
) -> dict:
    """The whole store as one document the view can render off-pod.

    One unreadable stream may not cost the others, so each stream's reads are
    wrapped and a failure becomes that stream's `error` plus `liveness:
    "error"` while the sweep continues.
    """
    if not isinstance(root, BrokerReportStore):
        root = reports_root(root)
    scratch = scratch_root(scratch)
    now_epoch = _now_epoch(now)
    # The broker store has no root directory to be absent: it is there when
    # it answers, and a listing it refuses is the `root_error` below.
    root_exists = isinstance(root, BrokerReportStore) or os.path.isdir(root)
    root_error: str | None = None
    try:
        ids = stream_ids(root)
    except OSError as exc:
        # A root that is present but cannot be listed is a store the view could
        # not read, not a fleet with no streams — and `root_exists` is the key
        # its exit code hangs on. `root_error` keeps the reason, without the
        # path — every reader already names `root` beside it.
        ids, root_exists = [], False
        root_error = os_reason(exc)
    lease_error: str | None = None
    try:
        in_flight = in_flight_ids(scratch)
    except OSError as exc:
        in_flight, lease_error = [], _failure(f"{scratch}/", exc)
    ids = sorted(set(ids) | set(in_flight))
    return {
        "root": str(root),
        "root_exists": root_exists,
        "root_error": root_error,
        "generated_at": datetime.fromtimestamp(now_epoch, timezone.utc).isoformat(),
        "ttl_s": INFLIGHT_TTL_S,
        # Also on every stream, but a store with no stream directory yet has no
        # stream to carry it, and a first run in flight is exactly what the
        # unlisted scratch directory would have shown.
        "lease_error": lease_error,
        "streams": {
            audit_id: project_stream(root, scratch, audit_id, now_epoch, lease_error)
            for audit_id in ids
        },
    }


def project_stream(
    root: str, scratch: str, audit_id: str, now_epoch: float, lease_error: str | None = None
) -> dict:
    """The stream's lease, and one entry per repository it has a store for.

    Liveness is the stream's, because the lease is: one `start` holds the
    stream across every repository. `error` names the lease when it could not
    be read, else the first repository that could not be, and the entry for
    that repository carries its own, so a reader shows a repository entry's
    error first and falls back to this one. `lease_error` is the scratch
    directory's listing failure, which `project` found once for every stream.
    """
    error = lease_error
    try:
        started = in_flight_since(scratch, audit_id)
    except OSError as exc:
        started, error = None, _failure("the in-flight note", exc)
    repos: dict[str, dict] = {}
    try:
        dirs, unreadable = scan_repo_dirs(root, audit_id)
    except OSError as exc:
        dirs, unreadable = [], []
        error = error or _failure(f"{audit_id}/", exc)
    # Only lower-case directories count: the writer spells every one so and
    # `store_path` opens nothing else, so a mixed-case one would list as a
    # repository that never ran. The error names those instead.
    ids = [repo for repo in dirs if repo == repo.lower()]
    strays = [repo for repo in dirs if repo != repo.lower()]
    if unreadable:
        error = error or "; ".join(unreadable)
    if strays:
        error = error or f"{', '.join(strays)}: not lower-case, so no reader opens it"
    any_latest = None
    for repo in ids:
        entry = _project_repo(root, audit_id, repo)
        repos[repo] = entry
        error = error or (f"{repo}: {entry['error']}" if entry["error"] else None)
        any_latest = any_latest or entry["latest"]
    return {
        "started": _project_started(started, now_epoch),
        "repos": repos,
        "liveness": liveness(started, any_latest, now_epoch, error=error),
        "error": error,
    }


def _project_repo(root: str, audit_id: str, repo: str) -> dict:
    latest: dict | None = None
    runs: list[str] = []
    error: str | None = None
    latest_missing = False
    try:
        latest, latest_missing = load_last(root, audit_id, repo)
    except RingReadError as exc:
        # `latest.json` is gone and the ring behind it is what failed.
        error, latest_missing = _failure(exc.name, exc), True
    except (OSError, ValueError) as exc:
        error = _failure(LATEST_NAME, exc)
    try:
        runs = list_runs(root, audit_id, repo)
    except OSError as exc:
        error = error or _failure("runs/", exc)
    return {
        "latest": _project_latest(latest),
        # True when `latest` is the newest ring entry because `latest.json` is
        # gone or older than it: a run after it failed, and the ledger may be
        # newer than this.
        "latest_missing": latest_missing,
        "runs": runs,
        "error": error,
    }


def _project_started(started: float | None, now_epoch: float) -> dict | None:
    if started is None:
        return None
    return {
        "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "age_s": round(now_epoch - started, 1),
    }


def _project_latest(envelope: dict | None) -> dict | None:
    """The last run's envelope minus the heavy keys, plus counts derived from it.

    Every count is guarded into null rather than a number, because a malformed
    envelope must read as "unknown" and not as "zero findings".
    """
    if envelope is None:
        return None
    row: dict = {key: envelope.get(key) for key in LATEST_KEYS}
    row.update(
        {
            key: value
            for key, value in envelope.items()
            if key not in _NEVER_PROJECTED and key not in row
        }
    )
    row["new"] = _count(envelope.get("new_ids"))
    row["resolved"] = _count(envelope.get("resolved_ids"))
    row["current"] = _count(envelope.get("current_ids"))
    document = envelope.get("document")
    document = document if isinstance(document, dict) else {}
    findings = document.get("findings")
    row["findings"] = _count(findings)
    row["critical"] = (
        sum(1 for finding in findings if _is_critical(finding))
        if isinstance(findings, list)
        else None
    )
    scope = document.get("scope")
    scope = scope if isinstance(scope, dict) else {}
    row["clusters"] = _count(scope.get("clusters"))
    row["skipped"] = _count(scope.get("skipped"))
    return row


def _is_critical(finding: object) -> bool:
    return (
        isinstance(finding, dict)
        and str(finding.get("severity", "")).strip().lower() == "critical"
    )


def _count(value: object) -> int | None:
    return len(value) if isinstance(value, list) else None


def store_root_error(root: str) -> str | None:
    """Why the store root cannot be listed, or None when it can or is absent.

    Absent is None because it is a different answer: nothing stored yet, not
    a store that could not be read.
    """
    try:
        stream_ids(root)
    except OSError as exc:
        return os_reason(exc)
    return None


def os_reason(exc: OSError) -> str:
    """The error without the path it names, for a caller that names it itself."""
    if exc.filename is not None and exc.strerror:
        return exc.strerror
    return " ".join(str(exc).split()) or type(exc).__name__


def _failure(label: str, exc: Exception) -> str:
    """One line, always — the view prints it in a table cell."""
    return f"{label}: {' '.join(str(exc).split()) or type(exc).__name__}"


def _now_epoch(now: float | datetime | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


# --------------------------------------------------------------------------- #
# The broker store (#5619): the same store over the credential broker's
# /v1/audit-reports, for a pod that cannot see the volume the store is on.
#
# Here rather than in a module of its own because this file imports nothing
# outside the standard library and no sibling (the module docstring): the
# view streams it into a pod on stdin, and audit_report.py and report_query.py
# import it for the client. `credential_proxy_client` is not importable from
# a skill, so its two header rules are restated below, stdlib-only.
# --------------------------------------------------------------------------- #

#: The switch. The operator renders `broker` on the shell sandbox's shell
#: container and on profile workers; unset, or anything else, is the file
#: store, as before the broker store existed.
REPORT_STORE_ENV = "FLEET_AUDIT_REPORT_STORE"
REPORT_STORE_BROKER = "broker"
# credential_proxy_client's names: the broker's address, the projected token
# that authenticates this caller, and a profile task's credential.
BROKER_URL_ENV = "CREDENTIAL_PROXY_URL"
BROKER_TOKEN_FILE_ENV = "CREDENTIAL_PROXY_TOKEN_FILE"  # noqa: S105 - an env name
TASK_TOKEN_FILE_ENV = "CREDENTIAL_PROXY_TASK_TOKEN_FILE"  # noqa: S105 - an env name
TASK_CREDENTIAL_HEADER = "X-Kube-Agents-Task-Credential"
# credential_proxy_client.MAX_TASK_CREDENTIAL_BYTES and its shape check.
MAX_TASK_CREDENTIAL_BYTES = 8192
_TASK_CREDENTIAL_SHAPE = re.compile(r"\A[A-Za-z0-9_.-]+\Z")
AUDIT_REPORTS_ROUTE = "/v1/audit-reports"
# Longer than the broker's own relay deadline (15 s), so its 504 arrives
# rather than this side giving up first.
BROKER_TIMEOUT_S = 20.0
# The broker's own answers for a store it cannot use now: 503 with no store
# configured (or a ConfigMap it cannot read), 502 for a listener that failed,
# 504 for one that did not answer in time.
UNAVAILABLE_STATUSES = frozenset({502, 503, 504})
# The ring's stamp, as the broker's route spells it (_REPORT_RUN_STAMP).
RUN_NAME_RE = re.compile(r"\A[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\.json\Z")
# The store listener's answer for a file that is not there
# (audit_report_store._read_at and handle), which the broker relays whole. The
# broker's own route refusal is `{"status": "not_found"}`, and the listener's
# unknown route `{"error": "no such route"}`: neither is a missing file.
LISTENER_NOT_FOUND = {"error": "not found"}
#: Left in a local stream directory once its envelopes are copied into the
#: broker store, so the copy happens once.
SEED_MARKER_NAME = ".seeded-to-broker"


class ReportStoreError(OSError):
    """The broker store refused or failed a request. An OSError, so every
    reader and writer that already handles a store it cannot read or write
    handles this one the same way. `status` is the HTTP status, None when no
    answer came."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ReportStoreUnavailable(ReportStoreError):
    """The store cannot be used now: no broker address or token, no answer,
    or the broker's 502, 503 or 504."""


def task_credential_headers(path: str) -> dict[str, str]:
    """credential_proxy_client.task_credential_headers, restated stdlib-only.

    The task credential is sent only when the file holds one, and a report
    credential (one that names no target) only on the audit-report routes.
    Every request this client makes is on those routes, so in practice: sent
    when present. A missing file is no task. A file that cannot be read, or
    whose value is not one compact JWT, raises ReportStoreUnavailable rather
    than send a request that would earn a refusal hiding the fault.
    """
    token_file = os.environ.get(TASK_TOKEN_FILE_ENV, "").strip()
    if not token_file:
        return {}
    try:
        with open(token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise ReportStoreUnavailable(
            f"the task credential file could not be read ({type(exc).__name__})"
        ) from None
    if not token:
        return {}
    if len(token) > MAX_TASK_CREDENTIAL_BYTES or not _TASK_CREDENTIAL_SHAPE.match(token):
        raise ReportStoreUnavailable("the task credential file does not hold a task credential")
    if _is_report_credential(token) and not _audit_reports_path(path):
        return {}
    return {TASK_CREDENTIAL_HEADER: token}


def _is_report_credential(token: str) -> bool:
    """credential_proxy_client._is_report_credential: claims read unverified."""
    parts = token.split(".")
    if len(parts) != 3:
        return False
    segment = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, RecursionError):
        return False
    nats = claims.get("nats") if isinstance(claims, dict) else None
    return isinstance(nats, dict) and "target" not in nats


def _audit_reports_path(path: str) -> bool:
    path = urllib.parse.urlsplit(path).path
    return path == AUDIT_REPORTS_ROUTE or path.startswith(AUDIT_REPORTS_ROUTE + "/")


def _bearer_headers() -> dict[str, str]:
    """credential_proxy_client.authorization_headers: read fresh on every call,
    because the kubelet rewrites a projected token in place."""
    token_file = os.environ.get(BROKER_TOKEN_FILE_ENV, "").strip()
    if not token_file:
        return {}
    try:
        with open(token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        raise ReportStoreUnavailable(
            f"the broker token file could not be read ({exc.strerror or type(exc).__name__})"
        ) from None
    if not token:
        raise ReportStoreUnavailable("the broker token file is empty")
    return {"Authorization": f"Bearer {token}"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect out of the broker (credential_proxy_client's
    rule): urllib re-sends the Authorization header across a cross-host
    redirect, so following one would hand the bearer and the task credential
    to whatever host the answer names. The 3xx comes back as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def _broker_opener() -> urllib.request.OpenerDirector:
    """An opener of this module's own for every broker call: no proxy from
    HTTP(S)_PROXY, which would see both credentials, and no redirect."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


class BrokerReportStore(str):
    """The report store, read and written through the broker's /v1/audit-reports.

    A `str` whose value names it (`broker <url>`), so a reader that prints or
    serialises the store root prints this; every reader that would open a path
    under the root asks `isinstance(root, BrokerReportStore)` first.

    The same names as the file store, by stream and repository: `latest.json`
    and the ring's `<stamp>.json`. A missing file is FileNotFoundError (the
    broker's 404); any other failure is ReportStoreError, ReportStoreUnavailable
    for 502/503/504 or no answer. The listener prunes the ring and refuses a
    `latest` that is not a byte copy of a ring entry.

    `local_root` is a file store on this pod, if there is one (the shell
    sandbox's, from before the switch). A stream it holds that the broker
    store does not is copied in once, the first time this client touches that
    stream (`_maybe_seed`).
    """

    url: str
    local_root: str | None

    def __new__(cls, url: str, local_root: str | None = None, log=None):
        self = super().__new__(cls, f"{REPORT_STORE_BROKER} {url or '(no ' + BROKER_URL_ENV + ')'}")
        self.url = (url or "").rstrip("/")
        self.local_root = local_root
        self._log = log or _stderr_line
        self._seed_checked: set[tuple[str, str]] = set()
        self._streams: list[dict] | None = None
        return self

    # -- the wire ----------------------------------------------------------- #

    def _request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        """One request; the body of a 2xx answer. Raises as the class says."""
        if not self.url:
            raise ReportStoreUnavailable(f"the report store is unavailable: {BROKER_URL_ENV} is unset")
        if urllib.parse.urlsplit(self.url).scheme not in ("http", "https"):
            raise ReportStoreUnavailable(f"the report store is unavailable: {BROKER_URL_ENV} is not an http(s) URL")
        headers = {**_bearer_headers(), **task_credential_headers(path)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=body, method=method, headers=headers)
        try:
            with _broker_opener().open(request, timeout=BROKER_TIMEOUT_S) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            answer = _answer_body(exc)
            exc.close()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise ReportStoreUnavailable(
                f"the report store at {self} is unavailable: {' '.join(str(reason).split())}"
            ) from None
        where = f"{method} {urllib.parse.unquote(path[len(AUDIT_REPORTS_ROUTE):]) or '/'}"
        detail = _answer_detail(answer)
        if status == 404 and answer == LISTENER_NOT_FOUND:
            raise FileNotFoundError(f"the report store has no {where}")
        if status in UNAVAILABLE_STATUSES or 300 <= status < 400 or status == 404:
            # Only the listener's own "not found", relayed whole, is a missing
            # file. Any other 404 is the broker refusing the route (`not_found`,
            # as a broker that predates the routes answers) or the listener
            # not knowing it: a store that can't be used now, never an empty
            # one, which would read as a first run.
            # A redirect is never followed (_NoRedirect): the store is not
            # where it should be, which is a store that can't be used now.
            raise ReportStoreUnavailable(
                f"the report store at {self} is unavailable: HTTP {status}{detail}", status
            )
        raise ReportStoreError(f"the report store refused {where}: HTTP {status}{detail}", status)

    def _stream_path(self, audit_id: str, repo: str, *rest: str) -> str:
        if not REPO_SEGMENT_RE.match(audit_id) or audit_id in (os.curdir, os.pardir):
            raise ValueError(f"stream {audit_id!r} is not a name inside the report store")
        return "/".join((AUDIT_REPORTS_ROUTE, audit_id, broker_store_key(repo), *rest))

    @staticmethod
    def _stamp(name: str) -> str:
        if not RUN_NAME_RE.match(name):
            raise ValueError(f"run {name!r} is not a report stamp")
        return name[: -len(".json")]

    # -- the store ---------------------------------------------------------- #

    def streams(self) -> list[dict]:
        """The broker's stream list: `audit_id`, `key`, `newest_run`,
        `latest_finished_at` per store. Read once per client, after copying in
        any local stream the broker does not hold."""
        if self._streams is None:
            self._seed_local_streams()
            listed = json.loads(self._request("GET", AUDIT_REPORTS_ROUTE).decode("utf-8"))
            if not isinstance(listed, list):
                raise ReportStoreError("the report store's stream list is not a list")
            self._streams = [row for row in listed if isinstance(row, dict)]
        return self._streams

    def stream_ids(self) -> list[str]:
        return sorted({str(row.get("audit_id")) for row in self.streams() if row.get("audit_id")})

    def repo_dirs(self, audit_id: str) -> list[str]:
        """The stream's repositories as `scan_repo_dirs` spells them."""
        names = []
        for row in self.streams():
            if row.get("audit_id") != audit_id or not isinstance(row.get("key"), str):
                continue
            owner, _, below = urllib.parse.unquote(row["key"]).partition("/")
            if owner and below:
                names.append(f"{owner}/{below.replace(STORE_PATH_SEPARATOR, '/')}")
        return sorted(names)

    def read_latest(self, audit_id: str, repo: str) -> bytes | None:
        """`latest.json`'s bytes; None when there is none."""
        self._maybe_seed(audit_id, repo)
        try:
            return self._request("GET", self._stream_path(audit_id, repo, "latest"))
        except FileNotFoundError:
            return None

    def list_runs(self, audit_id: str, repo: str) -> list[str]:
        """The ring's names, oldest first, as `list_runs` lists the directory."""
        self._maybe_seed(audit_id, repo)
        return self._ring(audit_id, repo)

    def _ring(self, audit_id: str, repo: str) -> list[str]:
        try:
            stamps = json.loads(self._request("GET", self._stream_path(audit_id, repo, "runs")).decode("utf-8"))
        except FileNotFoundError:
            return []
        if not isinstance(stamps, list):
            raise ReportStoreError("the report store's run list is not a list")
        return sorted(f"{stamp}.json" for stamp in stamps if isinstance(stamp, str))

    def read_run(self, audit_id: str, repo: str, name: str) -> bytes | None:
        """One ring entry's bytes; None when that stamp is not in the ring."""
        self._maybe_seed(audit_id, repo)
        try:
            return self._request("GET", self._stream_path(audit_id, repo, "runs", self._stamp(name)))
        except FileNotFoundError:
            return None

    def write_run(self, audit_id: str, repo: str, name: str, data: bytes) -> None:
        self._maybe_seed(audit_id, repo)
        self._request("PUT", self._stream_path(audit_id, repo, "runs", self._stamp(name)), data)
        self._streams = None

    def write_latest(self, audit_id: str, repo: str, data: bytes) -> None:
        self._maybe_seed(audit_id, repo)
        self._request("PUT", self._stream_path(audit_id, repo, "latest"), data)
        self._streams = None

    def delete_latest(self, audit_id: str, repo: str) -> None:
        """Drop `latest.json`. Absent already is not an error."""
        self._maybe_seed(audit_id, repo)
        try:
            self._request("DELETE", self._stream_path(audit_id, repo, "latest"))
        except FileNotFoundError:
            pass
        self._streams = None

    # -- the seed (Q-L2) ---------------------------------------------------- #

    def _seed_local_streams(self) -> None:
        """Copy in every local stream the broker store lacks, for a listing."""
        if not self.local_root or not os.path.isdir(self.local_root):
            return
        try:
            for audit_id in stream_ids(self.local_root):
                for repo in scan_repo_dirs(self.local_root, audit_id)[0]:
                    if repo == repo.lower():
                        self._maybe_seed(audit_id, repo)
        except (OSError, ValueError) as exc:
            self._log(f"report store: the local store at {self.local_root} could not be listed to seed the broker: {exc}")

    def _maybe_seed(self, audit_id: str, repo: str) -> None:
        """Copy this stream's local envelopes into the broker store, once.

        Only when there is a local file store holding the stream and it
        carries no SEED_MARKER_NAME. Under an flock on the local stream
        directory, so two seeds on this pod (and a `finish` here, whose writes
        seed first) take turns. Then, by what the broker's ring holds:

        - nothing, or only stamps the local ring holds (a seed that stopped
          part way): the local entries it lacks, newer than its newest, go
          oldest first, then `latest.json` (`_seed_latest`), then the marker;
        - stamps the local ring does not hold (a run wrote there since): when
          every local entry is already in it, the marker alone, so later runs
          skip the reads; otherwise nothing is copied and nothing marked.

        Never deletes or changes a local envelope. A stream whose writes the
        broker refuses (403: a repository the install does not register) is
        skipped and logged, with no marker, so a later registration can still
        seed it. Any other failure is logged and left for the next run, which
        resumes where this one stopped.
        """
        try:
            key = (audit_id, broker_store_key(repo))
        except ValueError:
            return
        if key in self._seed_checked:
            return
        self._seed_checked.add(key)
        if not self.local_root or not os.path.isdir(self.local_root):
            return
        local = store_path(self.local_root, audit_id, repo)
        if not os.path.isdir(local) or os.path.lexists(os.path.join(local, SEED_MARKER_NAME)):
            return
        label = f"{audit_id} in {'/'.join(_store_segments(repo)).replace(STORE_PATH_SEPARATOR, '/')}"
        try:
            lock = os.open(local, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            self._log(f"report store: not seeding {label} into the broker: {local} could not be opened ({exc})")
            return
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if os.path.lexists(os.path.join(local, SEED_MARKER_NAME)):
                return
            self._seed_locked(audit_id, repo, local, label)
        finally:
            os.close(lock)  # closing the descriptor drops the lock

    def _seed_locked(self, audit_id: str, repo: str, local: str, label: str) -> None:
        """`_maybe_seed`'s work, with the local stream directory locked."""
        try:
            runs = sorted(name for name in os.listdir(os.path.join(local, RUNS_DIR)) if RUN_NAME_RE.match(name))
        except FileNotFoundError:
            runs = []
        except OSError as exc:
            self._log(f"report store: not seeding {label} into the broker: the local ring could not be listed ({exc})")
            return
        try:
            with open(os.path.join(local, LATEST_NAME), "rb") as handle:
                latest: bytes | None = handle.read()
        except FileNotFoundError:
            latest = None
        except OSError as exc:
            self._log(f"report store: not seeding {label} into the broker: the local latest.json could not be read ({exc})")
            return
        if not runs and latest is None:
            return
        copied = 0
        try:
            remote = self._ring(audit_id, repo)
            if not set(remote) <= set(runs):
                # A run wrote this stream through the broker since: its ring is
                # not a partial copy of the local one, so nothing is resumed.
                if set(runs) <= set(remote):
                    self._leave_marker(local, label, {"seeded_to": self.url, "runs": 0, "latest": False})
                return
            if not remote and self._get_latest(audit_id, repo) is not None:
                # A `latest` with no ring is no state the listener writes;
                # leave it to a run rather than guess.
                return
            missing = [name for name in runs if name not in remote and (not remote or name > remote[-1])]
            for name in missing:
                with open(os.path.join(local, RUNS_DIR, name), "rb") as handle:
                    data = handle.read()
                try:
                    self._request("PUT", self._stream_path(audit_id, repo, "runs", self._stamp(name)), data)
                except ReportStoreError as exc:
                    if exc.status == 403:
                        self._log(
                            f"report store: not seeding {label} into the broker: it refused the write "
                            f"(HTTP 403: a repository this install does not register)"
                        )
                        return
                    if exc.status in (400, 409, 413) and not isinstance(exc, ReportStoreUnavailable):
                        # This one entry: not JSON, too large, or stamped in
                        # the future. The rest of the ring still copies.
                        self._log(f"report store: seeding {label}: skipped runs/{name} ({exc})")
                        continue
                    raise
                copied += 1
            latest_copied = self._seed_latest(audit_id, repo, local, runs, latest)
        except (OSError, ValueError) as exc:
            self._log(
                f"report store: seeding {label} into the broker stopped after {copied} run(s), "
                f"to be resumed by the next run: {exc}"
            )
            return
        self._streams = None
        self._leave_marker(local, label, {"seeded_to": self.url, "runs": copied, "latest": latest_copied})
        self._log(
            f"report store: seeded {label} into the broker from {local}: {copied} run(s), "
            f"latest {'copied' if latest_copied else 'not copied'}"
        )

    def _get_latest(self, audit_id: str, repo: str) -> bytes | None:
        try:
            return self._request("GET", self._stream_path(audit_id, repo, "latest"))
        except FileNotFoundError:
            return None

    def _seed_latest(self, audit_id: str, repo: str, local: str, runs: list[str], latest: bytes | None) -> bool:
        """PUT the local `latest.json` when it is a byte copy of the newest local
        entry and the broker has nothing newer; whether it was written.

        Checked again just before the write: a run that landed through the
        broker since (a ring entry newer than the newest local one, or a
        `latest` that is not this one) keeps its own, so a seed never rolls
        `latest` back.
        """
        if latest is None or not runs:
            return False
        with open(os.path.join(local, RUNS_DIR, runs[-1]), "rb") as handle:
            if handle.read() != latest:
                return False
        remote = self._ring(audit_id, repo)
        if not remote or remote[-1] != runs[-1]:
            return False
        current = self._get_latest(audit_id, repo)
        if current is not None:
            return current == latest
        self._request("PUT", self._stream_path(audit_id, repo, "latest"), latest)
        return True

    def _leave_marker(self, local: str, label: str, record: dict) -> None:
        try:
            with open(os.path.join(local, SEED_MARKER_NAME), "x", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except FileExistsError:
            pass
        except OSError as exc:
            self._log(f"report store: seeded {label} but could not leave {SEED_MARKER_NAME} in {local} ({exc})")


def _answer_body(exc: urllib.error.HTTPError) -> dict | None:
    """A refusal's JSON object body, or None."""
    try:
        body = json.loads(exc.read(4096).decode("utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _answer_detail(body: dict | None) -> str:
    """The broker's `code`, `rule` or `status` from a refusal body, for the log line."""
    if not isinstance(body, dict):
        return ""
    words = [str(body[key]) for key in ("code", "rule", "status") if isinstance(body.get(key), str)]
    return f" ({', '.join(words)})" if words else ""


def _stderr_line(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def select_report_store(root: str | None = None, log=None) -> tuple[str, str]:
    """The store a script uses, and the one line saying which and why.

    An explicit root (`--root`) is the file store at that root. Otherwise the
    broker store when FLEET_AUDIT_REPORT_STORE is `broker`, else the file
    store at `reports_root()`, exactly as before the switch existed. The line
    names the broker's URL, never a token.
    """
    if root:
        return str(root), f"report store: file {root} (--root)"
    choice = os.environ.get(REPORT_STORE_ENV, "").strip()
    if choice == REPORT_STORE_BROKER:
        store = BrokerReportStore(os.environ.get(BROKER_URL_ENV, "").strip(), reports_root(), log=log)
        return store, f"report store: {store} ({REPORT_STORE_ENV}={REPORT_STORE_BROKER})"
    why = f"{REPORT_STORE_ENV} is {choice!r}, not {REPORT_STORE_BROKER}" if choice else f"{REPORT_STORE_ENV} unset"
    local = reports_root()
    return local, f"report store: file {local} ({why})"


def report_store(root: str | None = None) -> str:
    """`select_report_store`, with its line logged on stderr: a script's one
    call, made where it picks its store."""
    store, line = select_report_store(root)
    _stderr_line(line)
    return store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Project the fleet-audit report store as one JSON document."
    )
    parser.add_argument(
        "--root",
        help=f"store root to read (default: $FLEET_AUDIT_REPORTS_DIR or {REPORTS_DIR})",
    )
    parser.add_argument(
        "--scratch",
        help=f"in-flight note directory (default: $FLEET_AUDIT_SCRATCH_DIR or {SCRATCH_DIR})",
    )
    args = parser.parse_args(argv)
    # Exit 0 even with no store: `root_exists: false` is the answer, and the
    # view decides what a missing store costs.
    print(json.dumps(project(report_store(args.root), scratch=args.scratch), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
