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

"""Open and close the seeded ledger of bench/tasks/fleet-audit-unread-target-keeps-finding.

main.tf runs this in the shell sandbox as
`python3 - seed <project> <location> <cluster> [<repo>]` or
`python3 - teardown [<repo>]`. The sandbox is where the audit worker runs
`audit_report.py`, so the forge and the report store that this script uses are
the ones the worker uses. It imports `audit_report` and uses only its
functions, so the ledger it opens is the ledger that `finish` writes, and the
stored record is one that `finish` trusts.

seed: opens one fleet-consistency-drift ledger with one finding on the host
cluster, under the `label-keys` facet, and writes the stored record of that
ledger. It also writes the issue number to `SEEDED_ISSUE_NAME` in the stream's
report store directory, which the stored record has made. The host cluster carries no environment label, so the drift collector
compares it on no facet: the next run has a coverage gap on the host and does
not check `label-keys` there. The cluster name is spelled as the drift
collector spells a target, `<project>/<location>/<name>`. Exits 1 when the
stream already has an open ledger (the harness reset closes it before the
run), or when the stored record does not pass the check that `finish` makes.
In that case it closes the issue it opened first: a failed apply runs no
destroy, so the destroy cannot close it.

teardown: closes, as not planned, the issue whose number the seed wrote, if
`issue-view` says it is open. Then it closes each other open ledger of the
stream that the label-filtered issue list gives. The number comes first
because on GitHub that list can omit an open issue that got its label a short
time before: the teardown then closed nothing, and the `finish` of the next
repetition opened a second ledger. The seed refuses when the stream has an
open ledger, so an open ledger at destroy is the one the seed opened. The run
under test can write that body again without the invented object (a run
without the fix does), so the teardown does not look for the name.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Where the sandbox image puts the skill scripts and the platform scripts they
# import (deploy/sandbox/smoke-test.sh imports audit_report the same way).
SKILL_SCRIPTS = "/opt/data/skills/fleet-audit/scripts"
PLATFORM_SCRIPTS = "/opt/data/scripts"
AUDIT_ID = "fleet-consistency-drift"
# A cluster-level facet: the drift collector declares it not applicable on no
# cluster whose cohort is under the floor, Standard or Autopilot.
HELD_CHECK = "label-keys"
HELD_SEVERITY = "minor"
# The invented object. No collector emits it, so only the ledger can name it,
# and the grade reads it in the ledger's hidden id block.
HELD_OBJECT_NAME = "eval-unread-held"
HELD_OBJECT = f"Cluster/{HELD_OBJECT_NAME}"
TARGET_SEPARATOR = "/"
SEED_STATUS = "OPENED"
TEARDOWN_REASON = "not-planned"
SEED_ARGS = 5
TEARDOWN_ARGS = 2
# audit_report's mark on a memory seeded from the ledger, not read from the store.
SEEDED_FROM_LEDGER_KEY = "seeded_from_ledger"
# The file in the stream's report store directory that holds the seeded issue
# number. Not a `.json` under `runs/`, so the store's readers and its prune
# do not see it.
SEEDED_ISSUE_NAME = "eval-unread-target-issue.json"
OPEN_STATE = "open"


def target_name(project, location, cluster):
    """The host cluster as the drift collector names a target."""
    return TARGET_SEPARATOR.join((project, location, cluster))


def seeded_document(audit_report, target, location, project):
    """The document of a run that found one `label-keys` finding on the host cluster."""
    checks = [
        {
            "check": check,
            "command": f"gcloud container clusters list --project {project} --format=json",
        }
        for check in audit_report.audit_checks(AUDIT_ID)
    ]
    finding = {
        "id": f"{HELD_CHECK}-{HELD_OBJECT_NAME}",
        "check": HELD_CHECK,
        "severity": HELD_SEVERITY,
        "title": "Cluster is missing label keys that its cohort carries",
        "cluster": target,
        "namespace": "",
        "object": HELD_OBJECT,
        "evidence": {
            "command": f"gcloud container clusters list --project {project} --format=json",
            "excerpt": '"resourceLabels": {}',
        },
        "impact": "Cost reports and policies that select on the cohort's labels do not see this cluster.",
        "recommendation": {
            "action": "Add the label keys that the cohort carries to the cluster.",
            "rationale": "The same label keys put the cluster in the same reports as its peers.",
            "risk": "None on apply; labels do not change the workloads.",
        },
        "remediation": {"kind": "manual", "note": "Set the cluster's resource labels."},
    }
    document = {
        "audit": AUDIT_ID,
        "scope": {
            "clusters": [
                {
                    "name": target,
                    "location": location,
                    "project": project,
                    "checks_run": checks,
                }
            ],
            "skipped": [],
        },
        "findings": [finding],
    }
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "findings.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return audit_report.load_findings(str(path), AUDIT_ID)


def seed(audit_report, project, location, cluster, repo, now):
    """Open the seeded ledger and store its record. Returns the issue URL."""
    number, *_ = audit_report.find_existing_issue(repo, AUDIT_ID)
    if number is not None:
        sys.exit(
            f"ERROR: {AUDIT_ID} already has open ledger #{number} in {repo}. The harness "
            "reset closes it before this stack runs; a run over it would not start from "
            "the seeded ledger this case grades."
        )
    target = target_name(project, location, cluster)
    data = seeded_document(audit_report, target, location, project)
    rendered = audit_report.render_issue_body(data, generated_at=now, audit_id=AUDIT_ID)
    audit_report.ensure_labels(repo, AUDIT_ID)
    opened = (
        audit_report.forge(
            "issue-create",
            repo,
            {
                "title": audit_report.issue_title(AUDIT_ID, data["findings"]),
                "body": rendered.body,
                "labels": ["agent:audit", f"audit:{AUDIT_ID}"],
            },
        ).get("issue")
        or {}
    )
    number = opened.get("number")
    url = str(opened.get("url") or "")
    if not isinstance(number, int):
        sys.exit(f"ERROR: the forge opened no issue for {AUDIT_ID} in {repo}: {opened}")
    stored = False
    try:
        stored = store_record(audit_report, data, rendered, number, url, repo, now)
    finally:
        if not stored:
            audit_report.forge("issue-close", repo, {"number": number, "reason": TEARDOWN_REASON})
    if not stored:
        sys.exit(
            f"ERROR: the stored record of #{number} was not written or is not one that "
            "finish trusts, so the run would treat the ledger as a lost memory and carry "
            f"nothing from it. Closed #{number}."
        )
    record_seeded_issue(audit_report, repo, number)
    return url


def seeded_issue_path(audit_report, repo):
    return audit_report.reports_dir_for(AUDIT_ID, repo) / SEEDED_ISSUE_NAME


def record_seeded_issue(audit_report, repo, number):
    """Write the seeded issue number where the teardown reads it.

    Only into a directory that is there: the stored record makes it, and this
    script does not make the report store. A number that is not written
    leaves the teardown with the issue list alone, so the failure is a warning.
    """
    path = seeded_issue_path(audit_report, repo)
    if not path.parent.is_dir():
        print(
            f"WARNING: no report store directory {path.parent}; #{number} is not recorded.",
            file=sys.stderr,
        )
        return
    try:
        path.write_text(
            json.dumps({"repo": repo, "number": number}) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        print(f"WARNING: could not record #{number} in {path}: {exc}", file=sys.stderr)


def recorded_seeded_issue(audit_report, repo):
    """The issue number the seed wrote for `repo`, or None."""
    try:
        record = json.loads(seeded_issue_path(audit_report, repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("repo") != repo:
        return None
    number = record.get("number")
    return number if isinstance(number, int) and not isinstance(number, bool) else None


def store_record(audit_report, data, rendered, number, url, repo, now):
    """Write the stored record of the seeded ledger. True when `finish` trusts it."""
    ids = audit_report.parse_delta_block(rendered.body)
    payload = {
        "status": SEED_STATUS,
        "issue_url": url,
        "new": len(ids),
        "resolved": 0,
        "partial": False,
        "coverage_gaps": [],
        "silent_ok": False,
    }
    audit_report.write_report(
        AUDIT_ID,
        audit_report.report_envelope(
            AUDIT_ID,
            payload,
            data,
            now,
            repo=repo,
            issue_number=number,
            ledger_body=rendered.body,
            new_ids=ids,
            resolved_ids=[],
            rendered_ids=ids,
        ),
        now,
    )
    # `write_report` never fails the run, and `previous_run_memory` seeds a
    # memory from the ledger when the store has no directory. So check that
    # the file is there and that the memory is the one written here.
    latest = audit_report.reports_dir_for(AUDIT_ID, repo) / audit_report.REPORT_LATEST_NAME
    memory = audit_report.previous_run_memory(AUDIT_ID, number, repo, rendered.body)
    return latest.is_file() and memory is not None and not memory.get(SEEDED_FROM_LEDGER_KEY)


def teardown(audit_report, repo):
    """Close the seeded ledger by its recorded number, then each other open ledger of the stream."""
    closed = []
    recorded = recorded_seeded_issue(audit_report, repo)
    if recorded is not None:
        issue = audit_report.forge("issue-view", repo, {"number": recorded}).get("issue") or {}
        if issue.get("state") == OPEN_STATE:
            audit_report.forge("issue-close", repo, {"number": recorded, "reason": TEARDOWN_REASON})
            closed.append(recorded)
    answer = audit_report.forge(
        "issue-list",
        repo,
        {"labels": [f"audit:{AUDIT_ID}"], "state": OPEN_STATE, "limit": audit_report.MAX_PAGE},
    )
    for issue in answer.get("issues") or []:
        if not isinstance(issue, dict) or not isinstance(issue.get("number"), int):
            continue
        if issue["number"] in closed:
            continue
        audit_report.forge(
            "issue-close", repo, {"number": issue["number"], "reason": TEARDOWN_REASON}
        )
        closed.append(issue["number"])
    try:
        seeded_issue_path(audit_report, repo).unlink(missing_ok=True)
    except OSError as exc:
        print(f"WARNING: could not remove the seeded issue record: {exc}", file=sys.stderr)
    return closed


def main(argv):
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "seed" and len(argv) >= SEED_ARGS:
        given = argv[SEED_ARGS] if len(argv) > SEED_ARGS else None
    elif mode == "teardown" and len(argv) >= TEARDOWN_ARGS:
        given = argv[TEARDOWN_ARGS] if len(argv) > TEARDOWN_ARGS else None
    else:
        sys.exit(
            "usage: ledger.py seed <project> <location> <cluster> [<repo>] | "
            "ledger.py teardown [<repo>]"
        )
    sys.path[:0] = [SKILL_SCRIPTS, PLATFORM_SCRIPTS]
    import audit_report  # noqa: PLC0415 -- the path is the sandbox's, set just above

    repo = audit_report.resolve_repo(AUDIT_ID, given)
    if mode == "seed":
        project, location, cluster = argv[2], argv[3], argv[4]
        url = seed(audit_report, project, location, cluster, repo, datetime.now(timezone.utc))
        print(f"Opened the seeded {AUDIT_ID} ledger {url} and stored its record.")
    else:
        closed = teardown(audit_report, repo)
        print(f"Closed {len(closed)} seeded {AUDIT_ID} ledger(s): {closed}")


if __name__ == "__main__":
    main(sys.argv)
