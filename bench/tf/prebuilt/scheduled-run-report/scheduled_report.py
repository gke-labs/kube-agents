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

seed: writes one run of the stream, an hour old, with one critical finding
whose cluster and namespace carry <mark>, through `audit_report.write_report`
as `finish` does: a ring entry and `latest`. A `latest` already there is moved
aside first and put back by teardown. Every write lands under
BROKER_STORE_ROOT. It refuses, writing nothing, when this container reads the
broker store itself, when its store root is not BROKER_STORE_ROOT, when an
earlier plant was not torn down, when the stream's ring has less than one free
slot (a real run landing during the case must not push a real one out), and
when the ring already holds a run at or after the planted one's time (the plant
must be the newest run, as `latest` is).

teardown: finds its notes under BROKER_STORE_ROOT/<audit id>/ by <mark>, so it
never resolves the repository again (a forge or broker hiccup at destroy time
must not send it to another directory). For each, it removes by exact path the
ring entry the seed wrote and `latest` while it is still the planted bytes,
puts back a `latest` the seed moved aside (or drops it when a newer real run
has replaced the plant), removes the directories the seed created when they
are empty, and removes the note. A note for another mark is left alone.
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
PLANT_SEVERITY = "critical"
PLANT_LOCATION = "us-central1"
# The seed's note of what it wrote, and the backup of a real `latest`, in the
# stream's store directory. Readers read only latest.json and runs/ there.
PLANT_NOTE = "eval-scheduled-run-plant.json"
LATEST_BACKUP = "eval-scheduled-run-latest.json"
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
    """The repository `start` resolves for the stream: the store directory a reader asks for."""
    return audit_report.resolve_reachable_repo(audit_id, repo or None).publish_repo


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
    """The plant note, rewritten as each write lands, so teardown always knows what to undo."""
    write_owned(note_path, json.dumps(note, sort_keys=True), note_path.parent.stat())


def refuse_unless_broker_root(audit_report):
    """Exit, writing nothing, unless this container writes the file store at BROKER_STORE_ROOT."""
    client = getattr(audit_report, "_report_store_client", lambda: None)()
    if client is not None:
        sys.exit(
            "ERROR: this container reads the broker report store itself; the plant writes the "
            "store's files, so it runs in the container that holds them."
        )
    root = os.environ.get("FLEET_AUDIT_REPORTS_DIR") or audit_report.REPORTS_DIR
    if os.path.realpath(root) != os.path.realpath(BROKER_STORE_ROOT):
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
    runs = store / audit_report.REPORT_RUNS_DIR
    real_runs = sorted(p.name for p in runs.glob("*.json")) if runs.is_dir() else []
    if len(real_runs) >= audit_report.REPORT_HISTORY - 1:
        sys.exit(
            f"ERROR: the {audit_id} store keeps {len(real_runs)} of {audit_report.REPORT_HISTORY} runs; the planted "
            "run and one real run landing during the case would push out a real one, which teardown cannot put back."
        )
    reported = now - REPORT_AGE
    ring_name = f"{stamp_of(audit_report, reported)}.json"
    if real_runs and real_runs[-1] >= ring_name:
        sys.exit(
            f"ERROR: the {audit_id} store already holds a run at or after {ring_name} ({real_runs[-1]}); the "
            "planted run would not be the newest."
        )
    # Built, and so checked, before anything on disk changes.
    data = planted_document(audit_report, audit_id, mark)
    # Every directory the seed is about to create, the store's missing parents
    # included and the root itself never, for teardown to remove when empty.
    root = Path(os.path.realpath(BROKER_STORE_ROOT))
    created = [str(runs)] if not runs.exists() else []
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
    # A store directory is <stream root>/<owner>/<name>, or deeper for a host-qualified key.
    for depth in ("*/*", "*/*/*", "*/*/*/*"):
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


def teardown_one(audit_report, note_path, note):
    """Undo one plant from its note. Returns the paths removed."""
    store = note_path.parent
    runs = store / audit_report.REPORT_RUNS_DIR
    latest = store / audit_report.REPORT_LATEST_NAME
    backup = store / LATEST_BACKUP
    removed = []
    ring = note.get("ring")
    # Only paths under this store directory: a note edited to name anything
    # else removes nothing.
    if ring and Path(ring).parent == runs and os.path.lexists(ring):
        os.unlink(ring)
        removed.append(ring)
    planted = note.get("latest_sha256")
    still_planted = latest.is_file() and (planted is None or sha256_of(latest) == planted)
    if planted is None and latest.is_file():
        # The seed stopped before it recorded the planted bytes: the plant is
        # `latest` only when it carries the mark.
        still_planted = note.get("mark", "") in latest.read_text(encoding="utf-8", errors="replace")
    if still_planted:
        os.unlink(latest)
        removed.append(str(latest))
    if note.get("backup") == str(backup) and os.path.isfile(backup):
        if latest.exists():
            # A newer real run replaced the plant; the moved-aside run is older.
            os.unlink(backup)
            removed.append(str(backup))
        else:
            os.replace(backup, latest)
    note_path.unlink()
    removed.append(str(note_path))
    for directory in sorted(note.get("created") or [], key=len, reverse=True):
        if not under_root(directory) or os.path.realpath(directory) == os.path.realpath(BROKER_STORE_ROOT):
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
