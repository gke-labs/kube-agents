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

"""Plant and remove the unfinished run of bench/tasks/fleet-audit-unfinished-run-reported.

main.tf runs this in the shell sandbox as `python3 - <mode> <audit id> [<repo>]`.
The sandbox is where the audit worker runs `audit_report.py`, so the report
store this script writes is the one `start`, `finish` and the readers use.

seed: writes one good `latest` for the stream, a CLEAN run on an invented
cluster an hour ago, through `audit_report.write_report` as `finish` does.
A `latest` already there is moved aside first and put back by teardown. Then
it writes a newer run-start record that is never marked done, through the
store client exactly as `start` does (`report_status.write_run_start`), so
the stream's last run reads as one that did not finish. On a tree from
before run-start records (no `write_run_start`), it writes the record's file
in the same shape in the file store, where nothing reads it: that is the red.

The seed refuses the broker store: no route can delete a run-start record, so
teardown could not remove the plant.

teardown: removes, by exact path, the record, the ring entry and `latest`
the seed wrote, puts back a `latest` the seed moved aside, removes the
directories the seed created when they are empty, and removes its own
note of those paths.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Where the sandbox image puts the skill scripts and the platform scripts they
# import (deploy/sandbox/smoke-test.sh imports audit_report the same way).
SKILL_SCRIPTS = "/opt/data/skills/fleet-audit/scripts"
PLATFORM_SCRIPTS = "/opt/data/scripts"
# The planted run id: a DNS-1123 label no `start` makes (theirs start with a
# task id, `cron-` or `local-`).
RUN_ID = "eval-unfinished-run"
# The planted report: a clean run on an invented cluster, an hour before the
# record, so the report is plainly the run before the one that did not finish.
CLEAN_CLUSTER = "eval-unfinished-cluster"
CLEAN_LOCATION = "us-central1"
CLEAN_PROJECT = "eval-unfinished-project"
CLEAN_STATUS = "CLEAN"
REPORT_AGE = timedelta(hours=1)
# Mirrors report_status.RUN_STARTS_DIR and RUN_START_RECEIVED_KEY, for the
# tree that has neither.
RUN_STARTS_DIR = "run-starts"
RECEIVED_KEY = "received_at"
# The seed's note of what it wrote, in the stream's store directory. Readers
# read only latest.json, runs/ and run-starts/ there.
PLANT_NOTE = "eval-unfinished-run-plant.json"
LATEST_BACKUP = "eval-unfinished-run-latest.json"
SEED_MODE = "seed"
TEARDOWN_MODE = "teardown"
MODES = (SEED_MODE, TEARDOWN_MODE)


def clean_document(audit_report, audit_id):
    """The validated document of a clean run on the invented cluster."""
    import tempfile  # noqa: PLC0415

    document = {
        "audit": audit_id,
        "scope": {
            "clusters": [
                {
                    "name": CLEAN_CLUSTER,
                    "location": CLEAN_LOCATION,
                    "project": CLEAN_PROJECT,
                    "checks_run": [
                        {"check": check, "command": f"kubectl --context {CLEAN_CLUSTER} get pods -A -o json"}
                        for check in audit_report.audit_checks(audit_id)
                    ],
                }
            ],
            "skipped": [],
        },
        "findings": [],
    }
    with tempfile.TemporaryDirectory() as scratch:
        findings = Path(scratch) / "findings.json"
        findings.write_text(json.dumps(document), encoding="utf-8")
        return audit_report.load_findings(str(findings), audit_id)


def publish_repo(audit_report, audit_id, repo):
    """The repository `start` resolves for the stream: the one its record goes to."""
    return audit_report.resolve_reachable_repo(audit_id, repo or None).publish_repo


def store_dir(audit_report, audit_id, repo):
    return audit_report.reports_dir_for(audit_id, repo)


def write_record(audit_report, audit_id, repo, now):
    """The run-start record, as `start` writes it. Returns its file."""
    import report_status  # noqa: PLC0415 -- on the path audit_report runs from

    started_at = now.astimezone(timezone.utc).isoformat()
    target = store_dir(audit_report, audit_id, repo) / RUN_STARTS_DIR / f"{RUN_ID}.json"
    if hasattr(report_status, "write_run_start"):
        record = report_status.run_start_record(RUN_ID, started_at, True)
        report_status.write_run_start(audit_report._run_start_store(audit_id, repo), audit_id, repo, record)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {"run_id": RUN_ID, "started_at": started_at, "scheduled": True, RECEIVED_KEY: started_at}
    target.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    return target


def seed(audit_report, audit_id, repo, now):
    """Plant the good `latest` and the unfinished run. Returns the note it wrote."""
    client = getattr(audit_report, "_report_store_client", lambda: None)()
    if client is not None:
        sys.exit(
            "ERROR: the install uses the broker report store, where no route deletes a "
            "run-start record; teardown could not remove this plant."
        )
    store = store_dir(audit_report, audit_id, repo)
    # Every directory the seed is about to create, the store's missing parents
    # included, for teardown to remove when it is empty again.
    created = [str(d) for d in (store / audit_report.REPORT_RUNS_DIR, store / RUN_STARTS_DIR) if not d.exists()]
    parent = store
    while not parent.exists():
        created.append(str(parent))
        parent = parent.parent
    latest = store / audit_report.REPORT_LATEST_NAME
    backup = store / LATEST_BACKUP
    store.mkdir(parents=True, exist_ok=True)
    if latest.is_file():
        os.replace(latest, backup)
    reported = now - REPORT_AGE
    data = clean_document(audit_report, audit_id)
    payload = {
        "status": CLEAN_STATUS,
        "issue_url": "",
        "new": 0,
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
            new_ids=[],
            resolved_ids=[],
            rendered_ids=[],
            scheduled=True,
        ),
        reported,
    )
    ring = store / audit_report.REPORT_RUNS_DIR / f"{reported.astimezone(timezone.utc).strftime(audit_report.REPORT_STAMP_FORMAT)}.json"
    if not latest.is_file() or not ring.is_file():
        sys.exit(f"ERROR: the planted report for {audit_id} in {repo} was not written.")
    record = write_record(audit_report, audit_id, repo, now)
    if not record.is_file():
        sys.exit(f"ERROR: the run-start record for {audit_id} in {repo} was not written.")
    note = {
        "record": str(record),
        "ring": str(ring),
        "latest": str(latest),
        "backup": str(backup) if backup.is_file() else None,
        "created": created,
    }
    note_path = store / PLANT_NOTE
    note_path.write_text(json.dumps(note, sort_keys=True), encoding="utf-8")
    return note_path


def teardown(audit_report, audit_id, repo):
    """Remove exactly what the seed wrote. Returns the paths removed."""
    store = store_dir(audit_report, audit_id, repo)
    note_path = store / PLANT_NOTE
    try:
        note = json.loads(note_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        note = {}
    removed = []
    # Without a note, only the record's own path, which no `start` writes.
    for key in ("record", "ring", "latest"):
        target = note.get(key) or (str(store / RUN_STARTS_DIR / f"{RUN_ID}.json") if key == "record" else None)
        if target and os.path.lexists(target):
            os.unlink(target)
            removed.append(target)
    backup = note.get("backup")
    if backup and os.path.isfile(backup):
        os.replace(backup, note["latest"])
    if note_path.exists():
        note_path.unlink()
        removed.append(str(note_path))
    for directory in sorted(note.get("created") or [], key=len, reverse=True):
        try:
            os.rmdir(directory)
            removed.append(directory)
        except OSError:
            pass
    return removed


def main(argv):
    if len(argv) < 3 or argv[1] not in MODES:
        sys.exit(f"usage: run_start.py {{{'|'.join(MODES)}}} <audit id> [<repo>]")
    sys.path[:0] = [SKILL_SCRIPTS, PLATFORM_SCRIPTS]
    import audit_report  # noqa: PLC0415 -- the path is the sandbox's, set just above

    mode, audit_id = argv[1], argv[2]
    repo = publish_repo(audit_report, audit_id, argv[3] if len(argv) > 3 else None)
    if mode == SEED_MODE:
        note = seed(audit_report, audit_id, repo, datetime.now(timezone.utc))
        print(f"Planted a clean {audit_id} report and an unfinished run after it ({note}).")
    else:
        removed = teardown(audit_report, audit_id, repo)
        print(f"Removed the {audit_id} unfinished-run plant: {removed}")


if __name__ == "__main__":
    main(sys.argv)
