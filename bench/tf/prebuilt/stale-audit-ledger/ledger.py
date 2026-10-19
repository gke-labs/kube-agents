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

"""Open and close the stale ledger of bench/tasks/fleet-audit-clean-close-reads-clean.

main.tf runs this in the shell sandbox as `python3 - <mode> <audit id> [<repo>]`.
The sandbox is where the audit worker runs `audit_report.py`, so the forge and
the report store that this script uses are the ones the worker uses. It imports
`audit_report` and uses only its functions, so the ledger it opens is the ledger
that `finish` writes, and the stored record is one that `finish` trusts.

seed: opens one ledger for the stream with one finding on a cluster that does
not exist (a cluster deleted since that run), and writes the stored record of
that ledger. A clean run of the stream does not see that cluster, so nothing
holds the ledger open and the run closes it. Exits 1 when the stream already
has an open ledger (the harness reset closes it before the run), or when the
stored record does not pass the check that `finish` makes.

teardown: closes, as not planned, each open ledger of the stream whose body
names the invented cluster. Only this case writes that name.
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
# Invented names. No eval project has this cluster, so no run reads it.
RETIRED_CLUSTER = "eval-retired-cluster"
RETIRED_LOCATION = "us-central1"
RETIRED_PROJECT = "eval-retired-project"
RETIRED_NAMESPACE = "eval-retired-models"
RETIRED_OBJECT = "Deployment/eval-retired-model-server"
STALE_CHECK = "model-image-floating-tag"
STALE_SEVERITY = "minor"
SEED_STATUS = "OPENED"
TEARDOWN_REASON = "not-planned"
MODES = ("seed", "teardown")
# audit_report's mark on a memory seeded from the ledger, not read from the store.
SEEDED_FROM_LEDGER_KEY = "seeded_from_ledger"


def stale_document(audit_report, audit_id):
    """The document of a run that found one finding on the retired cluster."""
    checks = [
        {
            "check": check,
            "command": f"kubectl --context {RETIRED_CLUSTER} get deployments --all-namespaces -o json",
        }
        for check in audit_report.audit_checks(audit_id)
    ]
    finding = {
        "id": f"{STALE_CHECK}-{RETIRED_CLUSTER}",
        "check": STALE_CHECK,
        "severity": STALE_SEVERITY,
        "title": "Model server image uses a floating tag",
        "cluster": RETIRED_CLUSTER,
        "namespace": RETIRED_NAMESPACE,
        "object": RETIRED_OBJECT,
        "evidence": {
            "command": (
                f"kubectl --context {RETIRED_CLUSTER} get deployment "
                f"eval-retired-model-server -n {RETIRED_NAMESPACE} -o json"
            ),
            "excerpt": '"image": "vllm/vllm-openai:latest"',
        },
        "impact": "A new image under the same tag can change the model server without review.",
        "recommendation": {
            "action": "Pin the model server image to a digest.",
            "rationale": "A digest names one image, so a rollout cannot change it silently.",
            "risk": "None on apply; the running image does not change.",
        },
        "remediation": {"kind": "manual", "note": "Set the image to a digest in the Deployment."},
    }
    document = {
        "audit": audit_id,
        "scope": {
            "clusters": [
                {
                    "name": RETIRED_CLUSTER,
                    "location": RETIRED_LOCATION,
                    "project": RETIRED_PROJECT,
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
        return audit_report.load_findings(str(path), audit_id)


def seed(audit_report, audit_id, repo, now):
    """Open the stale ledger and store its record. Returns the issue URL."""
    number, *_ = audit_report.find_existing_issue(repo, audit_id)
    if number is not None:
        sys.exit(
            f"ERROR: {audit_id} already has open ledger #{number} in {repo}. The harness "
            "reset closes it before this stack runs; a run over it would not start from "
            "the stale ledger this case grades."
        )
    data = stale_document(audit_report, audit_id)
    rendered = audit_report.render_issue_body(data, generated_at=now, audit_id=audit_id)
    audit_report.ensure_labels(repo, audit_id)
    opened = (
        audit_report.forge(
            "issue-create",
            repo,
            {
                "title": audit_report.issue_title(audit_id, data["findings"]),
                "body": rendered.body,
                "labels": ["agent:audit", f"audit:{audit_id}"],
            },
        ).get("issue")
        or {}
    )
    number = opened.get("number")
    url = str(opened.get("url") or "")
    if not isinstance(number, int):
        sys.exit(f"ERROR: the forge opened no issue for {audit_id} in {repo}: {opened}")
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
        audit_id,
        audit_report.report_envelope(
            audit_id,
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
    latest = audit_report.reports_dir_for(audit_id, repo) / audit_report.REPORT_LATEST_NAME
    memory = audit_report.previous_run_memory(audit_id, number, repo, rendered.body)
    if not latest.is_file() or memory is None or memory.get(SEEDED_FROM_LEDGER_KEY):
        sys.exit(
            f"ERROR: the stored record of #{number} was not written or is not one that "
            "finish trusts, so the run would hold the ledger open over a lost memory "
            "instead of closing it."
        )
    return url


def teardown(audit_report, audit_id, repo):
    """Close each open ledger of the stream that this case opened."""
    answer = audit_report.forge(
        "issue-list",
        repo,
        {"labels": [f"audit:{audit_id}"], "state": "open", "limit": audit_report.MAX_PAGE},
    )
    closed = []
    for issue in answer.get("issues") or []:
        if isinstance(issue, dict) and RETIRED_CLUSTER in str(issue.get("body") or ""):
            audit_report.forge(
                "issue-close", repo, {"number": issue["number"], "reason": TEARDOWN_REASON}
            )
            closed.append(issue["number"])
    return closed


def main(argv):
    if len(argv) < 3 or argv[1] not in MODES:
        sys.exit(f"usage: ledger.py {{{'|'.join(MODES)}}} <audit id> [<repo>]")
    sys.path[:0] = [SKILL_SCRIPTS, PLATFORM_SCRIPTS]
    import audit_report  # noqa: PLC0415 -- the path is the sandbox's, set just above

    mode, audit_id = argv[1], argv[2]
    repo = audit_report.resolve_repo(audit_id, argv[3] if len(argv) > 3 else None)
    if mode == "seed":
        url = seed(audit_report, audit_id, repo, datetime.now(timezone.utc))
        print(f"Opened the stale {audit_id} ledger {url} and stored its record.")
    else:
        closed = teardown(audit_report, audit_id, repo)
        print(f"Closed {len(closed)} stale {audit_id} ledger(s): {closed}")


if __name__ == "__main__":
    main(sys.argv)
