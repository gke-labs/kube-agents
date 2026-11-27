# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Plant and remove a scheduled run's report in the broker report store.

main.tf runs this in the gateway pod's `platform-agent` container as
`python3 - <mode> <audit id> <mark> [<repo>]`. That container keeps the file
store under the root that `audit_report_store.py` serves to the credential
broker (BROKER_STORE_ROOT), so a report written here is one the broker
answers, as a scheduled run on a profile worker leaves it (#6110). The shell
sandbox reads it only when it reads the broker store.

seed: writes one run of the stream with one critical finding whose cluster
and namespace carry <mark>, through `audit_report.write_report` as `finish`
does: a ring entry and `latest`. It's stamped an hour back, or just after the
newest real run when that is newer, so the plant is the newest run, as
`latest` is, and a real run in the last hour doesn't stop the case. When the
ring has less than two free slots (the steady state of a busy stream), the
oldest real entries are moved aside, each with its writer record, so the plant
and one real run landing during the case still fit and no real run is pruned. A `latest` already there
is moved aside too. Every write lands under BROKER_STORE_ROOT. It refuses,
writing nothing, when this container reads the broker store itself, when its
store root is not BROKER_STORE_ROOT (by text and by real path), when an
earlier plant was not torn down, and when the ring holds a run not before now.

teardown: finds its notes under BROKER_STORE_ROOT/<audit id>/ by <mark>, so it
never resolves the repository again (a forge or broker hiccup at destroy time
must not send it to another directory). For each, it removes by exact path the
ring entry the seed wrote, puts back the ring entries it moved aside, replaces
the planted `latest` with the one it moved aside in one move (or drops that
one when a real run replaced or deleted the plant), removes the directories
the seed created when they are empty, and removes the note. Paths are
compared by real path. A note for another mark is left alone, and a note
naming a path outside its own directory is kept after teardown.
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Where the image puts the skill scripts and the platform scripts they import.
SKILL_SCRIPTS = "/opt/data/skills/fleet-audit/scripts"
PLATFORM_SCRIPTS = "/opt/data/scripts"
# The root the listener serves to the broker (audit_report_store.py:
# DEFAULT_REPORTS_DIR), which it does not read from the environment.
BROKER_STORE_ROOT = "/opt/data/fleet-audit/reports"
# The planted run: an hour before the turn, so it reads as the last scheduled run.
REPORT_AGE = timedelta(hours=1)
PLANT_STATUS = "OPENED"
PLANT_CHECK = "netpol-missing"
# The one stream whose roster has PLANT_CHECK (variables.tf validates audit_id to it).
PLANT_AUDIT = "compliance-audit"
PLANT_SEVERITY = "critical"
PLANT_LOCATION = "us-central1"
# The seed's note of what it wrote, and the backup of a real `latest`, in the
# stream's store directory. Readers read only latest.json and runs/ there.
PLANT_NOTE = "eval-scheduled-run-plant.json"
LATEST_BACKUP = "eval-scheduled-run-latest.json"
# Where the seed moves the oldest real ring entries, in the stream's store
# directory, to make room for the plant and one real run landing during the
# case; teardown puts them back. Readers read only latest.json and runs/.
RING_ASIDE = "eval-scheduled-run-ring-aside"
# How far after the newest real run the plant is stamped when that run is
# newer than REPORT_AGE, so the plant is the newest run, as `latest` is.
AFTER_NEWEST = timedelta(seconds=1)
# A ring entry's name, as the listener reads the ring (audit_report_store.py:
# STAMP_RE); any other file in runs/ is not a run.
STAMP_RE = re.compile(r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\Z")
# The broker store's writer record beside a ring entry (audit_report_store.py:
# REPORT_WRITER_SUFFIX). Its prune deletes a record whose entry is not in the
# ring, so a record moves aside with its entry.
WRITER_SUFFIX = ".writer"
# A mark names the planted cluster, project and namespace and the finding id,
# so it must be a DNS-1123 label of 3 to 32 characters, short enough to take
# their suffixes.
MARK_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
SEED_MODE = "seed"
TEARDOWN_MODE = "teardown"
MODES = (SEED_MODE, TEARDOWN_MODE)


def names(mark):
    """The planted cluster, project and namespace: each carries `mark`."""
    return {"cluster": f"{mark}-cluster", "project": f"{mark}-project", "namespace": f"{mark}-payments"}


def planted_document(audit_report, audit_id, mark):
    """The validated document of a run with one critical finding on the marked cluster."""
    if PLANT_CHECK not in audit_report.audit_checks(audit_id):
        sys.exit(f"ERROR: the planted finding's check {PLANT_CHECK} is not in the {audit_id} roster; plant {PLANT_AUDIT}.")
    planted = names(mark)
    command = f"kubectl --context {planted['cluster']} get networkpolicy -n {planted['namespace']}"
    document = {
        "audit": audit_id,
        "scope": {
            "clusters": [
                {
                    "name": planted["cluster"],
                    "location": PLANT_LOCATION,
                    "project": planted["project"],
                    "checks_run": [
                        {"check": check, "command": f"kubectl --context {planted['cluster']} get pods -A -o json"}
                        for check in audit_report.audit_checks(audit_id)
                    ],
                }
            ],
            "skipped": [],
        },
        "findings": [
            {
                "id": f"{mark}-netpol",
                "check": PLANT_CHECK,
                "severity": PLANT_SEVERITY,
                "title": f"Namespace {planted['namespace']} has no NetworkPolicy",
                "cluster": planted["cluster"],
                "namespace": planted["namespace"],
                "object": f"Namespace/{planted['namespace']}",
                "evidence": {"command": command, "excerpt": f"No resources found in {planted['namespace']} namespace."},
                "impact": f"All pod-to-pod traffic in {planted['namespace']} is unrestricted.",
                "recommendation": {
                    "action": f"Apply a default-deny NetworkPolicy to the {planted['namespace']} namespace.",
                    "rationale": "Namespace-scoped default-deny is the smallest change that closes east-west exposure.",
                    "risk": "Unlabelled cross-namespace traffic into the namespace breaks on apply.",
                },
                "remediation": {"kind": "manual", "note": "Apply a default-deny NetworkPolicy."},
            }
        ],
    }
    with tempfile.TemporaryDirectory() as scratch:
        findings = Path(scratch) / "findings.json"
        findings.write_text(json.dumps(document), encoding="utf-8")
        return audit_report.load_findings(str(findings), audit_id)


def publish_repo(audit_report, audit_id, repo):
    """The stream's repository as the install registers it: the store directory a reader asks for.

    `resolve_repo_or_no_forge`, not the reachability probe `start` makes: the
    gateway's `platform-agent` container has no broker URL, so every forge
    probe from here fails, and the probe's fallback would plant where no
    reader looks.
    """
    return audit_report.resolve_repo_or_no_forge(audit_id, repo or None)


def under_root(path):
    """Whether `path` is BROKER_STORE_ROOT or below it, after resolving links."""
    root = os.path.realpath(BROKER_STORE_ROOT)
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def write_owned(path, text, owner):
    """Write `path`, owned like `owner` when this run is root.

    The open refuses a symlink at `path`, and the chown goes through the open
    descriptor, never the path.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        if os.geteuid() == 0:
            os.fchown(handle.fileno(), owner.st_uid, owner.st_gid)
        handle.write(text)


def write_note(note_path, note):
    """The plant note, rewritten as each write lands, so teardown always knows what to undo.

    Written to a temporary file beside it and moved into place, so the note on
    disk is always absent or a whole JSON object naming its mark: a write that
    fails part way (a full volume, a killed exec) leaves the last whole note.
    """
    temporary = note_path.with_name(f".{note_path.name}.{os.getpid()}.tmp")
    try:
        write_owned(temporary, json.dumps(note, sort_keys=True), note_path.parent.stat())
        os.replace(temporary, note_path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def refuse_unless_broker_root(audit_report):
    """Exit, writing nothing, unless this container writes the file store at BROKER_STORE_ROOT."""
    client = getattr(audit_report, "_report_store_client", lambda: None)()
    if client is not None:
        sys.exit(
            "ERROR: this container reads the broker report store itself; the plant writes the "
            "store's files, so it runs in the container that holds them."
        )
    root = os.environ.get("FLEET_AUDIT_REPORTS_DIR") or audit_report.REPORTS_DIR
    # By its text as well as its real path, so every path the note records is
    # spelled from BROKER_STORE_ROOT, as teardown builds its own.
    if os.path.normpath(root) != os.path.normpath(BROKER_STORE_ROOT) or os.path.realpath(root) != os.path.realpath(BROKER_STORE_ROOT):
        sys.exit(f"ERROR: this container's report store is {root}, not the broker store's root {BROKER_STORE_ROOT}.")


def stamp_of(audit_report, when):
    return when.astimezone(timezone.utc).strftime(audit_report.REPORT_STAMP_FORMAT)


def sha256_of(path):
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def seed(audit_report, audit_id, mark, repo, now):
    """Plant the marked run. Returns the note it wrote.

    The planted document is built before anything moves, and the note is
    written before each change it records, so a seed that fails part way
    leaves teardown everything it needs.
    """
    refuse_unless_broker_root(audit_report)
    store = audit_report.reports_dir_for(audit_id, repo)
    if not under_root(store):
        sys.exit(f"ERROR: the {audit_id} store for {repo} ({store}) is outside {BROKER_STORE_ROOT}.")
    latest = store / audit_report.REPORT_LATEST_NAME
    backup = store / LATEST_BACKUP
    note_path = store / PLANT_NOTE
    for leftover in (note_path, backup):
        if os.path.lexists(leftover):
            sys.exit(f"ERROR: an earlier plant was not torn down ({leftover} is still there); run teardown first.")
    aside_dir = store / RING_ASIDE
    if os.path.lexists(aside_dir):
        sys.exit(f"ERROR: an earlier plant was not torn down ({aside_dir} is still there); run teardown first.")
    runs = store / audit_report.REPORT_RUNS_DIR
    real_runs = sorted(p.name for p in runs.glob("*.json") if STAMP_RE.match(p.stem)) if runs.is_dir() else []
    # Stamped an hour back, or just after the newest real run when that is
    # newer: a real run of the stream in the last hour must not stop the case.
    reported = now - REPORT_AGE
    if real_runs:
        newest = datetime.strptime(real_runs[-1][: -len(".json")], audit_report.REPORT_STAMP_FORMAT).replace(
            tzinfo=timezone.utc
        )
        if newest >= reported:
            reported = newest + AFTER_NEWEST
        if reported >= now:
            sys.exit(
                f"ERROR: the {audit_id} store already holds a run at {real_runs[-1]}, not before now; the planted "
                "run would not be the newest."
            )
    ring_name = f"{stamp_of(audit_report, reported)}.json"
    # The oldest real entries to move aside, so the plant and one real run
    # landing during the case still fit the ring and no real run is pruned.
    aside = real_runs[: max(0, len(real_runs) + 2 - audit_report.REPORT_HISTORY)]
    # Built, and so checked, before anything on disk changes.
    data = planted_document(audit_report, audit_id, mark)
    # Every directory the seed is about to create, the store's missing parents
    # included and the root itself never, for teardown to remove when empty.
    root = Path(os.path.realpath(BROKER_STORE_ROOT))
    created = [str(runs)] if not runs.exists() else []
    if aside:
        created.append(str(aside_dir))
    parent = store
    while not parent.exists() and Path(os.path.realpath(parent)) != root:
        created.append(str(parent))
        parent = parent.parent
    audit_report._make_store_dirs(store)
    ring = runs / ring_name
    note = {
        "mark": mark,
        "audit_id": audit_id,
        "ring": str(ring),
        "latest": str(latest),
        "latest_sha256": None,
        "backup": None,
        "aside": [],
        "created": created,
    }
    try:
        write_note(note_path, note)
    except BaseException:
        for directory in sorted(created, key=len, reverse=True):
            try:
                os.rmdir(directory)
            except OSError:
                pass
        raise
    if aside:
        audit_report._make_store_dirs(aside_dir)
    for name in aside:
        # Each named in the note before its move: teardown checks it exists.
        note["aside"].append(name)
        write_note(note_path, note)
        writer = (runs / name).with_suffix(WRITER_SUFFIX)
        if writer.is_file():
            os.replace(writer, aside_dir / writer.name)
        os.replace(runs / name, aside_dir / name)
    if latest.is_file():
        # Named in the note before the move: teardown checks the backup exists.
        note["backup"] = str(backup)
        write_note(note_path, note)
        os.replace(latest, backup)
    payload = {
        "status": PLANT_STATUS,
        "issue_url": "",
        "new": 1,
        "resolved": 0,
        "partial": False,
        "coverage_gaps": [],
        "silent_ok": False,
    }
    audit_report.write_report(
        audit_id,
        audit_report.report_envelope(
            audit_id,
            payload,
            data,
            reported,
            repo=repo,
            issue_number=None,
            ledger_body="",
            new_ids=[f["id"] for f in data["findings"]],
            resolved_ids=[],
            rendered_ids=[f["id"] for f in data["findings"]],
            scheduled=True,
        ),
        reported,
    )
    if not latest.is_file() or not ring.is_file() or mark not in latest.read_text(encoding="utf-8"):
        sys.exit(f"ERROR: the planted report for {audit_id} in {repo} was not written.")
    note["latest_sha256"] = sha256_of(latest)
    write_note(note_path, note)
    return note_path


def notes_for(audit_id, mark):
    """Each plant note of `mark` under BROKER_STORE_ROOT/<audit_id>/, found without resolving a repository."""
    stream_root = Path(BROKER_STORE_ROOT) / audit_id
    found = []
    # A store directory is always two levels below the stream root
    # (`reports_dir_for`: <owner>/<name>, or <host>/<path with %2F>).
    for depth in ("*/*",):
        for note_path in sorted(stream_root.glob(f"{depth}/{PLANT_NOTE}")):
            if not under_root(note_path):
                continue
            try:
                note = json.loads(note_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                note = {}
            if note.get("mark") == mark:
                found.append((note_path, note))
    return found


def same_path(a, b):
    """Whether two paths name the same file or directory, by their real paths."""
    return os.path.realpath(a) == os.path.realpath(b)


def teardown_one(audit_report, note_path, note):
    """Undo one plant from its note. Returns the paths removed.

    Every path is compared by its real path with the one teardown builds from
    the note's own directory: a note edited to name anything else removes
    nothing, and is kept, so nothing it names is forgotten.
    """
    store = note_path.parent
    runs = store / audit_report.REPORT_RUNS_DIR
    latest = store / audit_report.REPORT_LATEST_NAME
    backup = store / LATEST_BACKUP
    aside_dir = store / RING_ASIDE
    removed = []
    skipped = False
    ring = note.get("ring")
    if ring:
        if same_path(Path(ring).parent, runs) and Path(ring).name.endswith(".json"):
            if os.path.lexists(runs / Path(ring).name):
                os.unlink(runs / Path(ring).name)
                removed.append(ring)
        else:
            skipped = True
    for name in note.get("aside") or []:
        held = aside_dir / Path(str(name)).name
        if Path(str(name)).name != name or not name.endswith(".json"):
            skipped = True
        else:
            held_writer = held.with_suffix(WRITER_SUFFIX)
            if os.path.isfile(held):
                if os.path.lexists(runs / name):
                    os.unlink(held)
                else:
                    os.replace(held, runs / name)
            if os.path.isfile(held_writer):
                if os.path.lexists(runs / held_writer.name):
                    os.unlink(held_writer)
                else:
                    os.replace(held_writer, runs / held_writer.name)
    planted = note.get("latest_sha256")
    still_planted = latest.is_file() and (planted is None or sha256_of(latest) == planted)
    if planted is None and latest.is_file():
        # The seed stopped before it recorded the planted bytes: the plant is
        # `latest` only when it carries the mark.
        still_planted = note.get("mark", "") in latest.read_text(encoding="utf-8", errors="replace")
    named_backup = note.get("backup")
    backup_is_ours = bool(named_backup) and same_path(named_backup, backup)
    if named_backup and not backup_is_ours:
        skipped = True
    if backup_is_ours and os.path.isfile(backup):
        if still_planted:
            # One move over the plant: no moment with neither in place.
            os.replace(backup, latest)
            removed.append(str(latest))
        elif latest.exists() or planted is not None:
            # A real run replaced the plant, or deleted `latest` on purpose
            # (`invalidate_report_memory`, a failed `write_report`: absent is
            # honest, stale is not). The moved-aside run is older either way.
            os.unlink(backup)
            removed.append(str(backup))
        else:
            os.replace(backup, latest)
    elif still_planted:
        os.unlink(latest)
        removed.append(str(latest))
    if skipped:
        return removed
    note_path.unlink()
    removed.append(str(note_path))
    for directory in sorted(note.get("created") or [], key=len, reverse=True):
        if not under_root(directory) or same_path(directory, BROKER_STORE_ROOT):
            continue
        try:
            os.rmdir(directory)
            removed.append(directory)
        except OSError:
            pass
    return removed


def teardown(audit_report, audit_id, mark):
    """Remove exactly what the seed of `mark` wrote. Returns the paths removed."""
    removed = []
    for note_path, note in notes_for(audit_id, mark):
        removed += teardown_one(audit_report, note_path, note)
    return removed


def main(argv):
    if len(argv) < 4 or argv[1] not in MODES:
        sys.exit(f"usage: scheduled_report.py {{{'|'.join(MODES)}}} <audit id> <mark> [<repo>]")
    mode, audit_id, mark = argv[1], argv[2], argv[3]
    if not MARK_RE.match(mark):
        sys.exit(f"ERROR: mark {mark!r} is not a DNS-1123 label of 3 to 32 characters.")
    sys.path[:0] = [SKILL_SCRIPTS, PLATFORM_SCRIPTS]
    import audit_report  # noqa: PLC0415 -- the path is the image's, set just above

    if mode == SEED_MODE:
        repo = publish_repo(audit_report, audit_id, argv[4] if len(argv) > 4 else None)
        note = seed(audit_report, audit_id, mark, repo, datetime.now(timezone.utc))
        print(f"Planted a scheduled {audit_id} run marked {mark} in the broker store ({note}).")
    else:
        removed = teardown(audit_report, audit_id, mark)
        print(f"Removed the {audit_id} scheduled-run plant marked {mark}: {removed}")


if __name__ == "__main__":
    main(sys.argv)
