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

"""Plant and remove the previous run of bench/tasks/fleet-audit-unchanged-finding-keeps-wording.

main.tf runs this in the container that runs `audit_report.py finish`, as
`python3 - <mode> <scripts dir> <mark>`. That container holds the report store
on its own volume.

plant: runs the drift collector, which reads GKE metadata only, and writes a
report-store envelope for the fleet-consistency-drift stream. The envelope holds
one finding for each candidate. Each finding has the id, the evidence and the
severity that `finish` gives the same candidate, and a title, impact,
recommendation and manual note that start with <mark>. A worker does not write
<mark>, so <mark> in the published ledger shows that `finish` kept the stored
wording. The envelope names no issue, so the delta of the next run does not
trust it. Exits 1 when the collector gives no candidate: then the case cannot
go green or red for the reason it tests.

teardown: removes each stored envelope of the stream, `latest.json` and the
ring entries, that holds <mark>. A kept wording then does not stay in the
store for later runs of the stream.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

AUDIT = "fleet-consistency-drift"
COLLECTOR = "fleet_drift.py"
CREDENTIAL_PROXY_BIN = "/opt/credential-proxy/bin"
# `finish` keeps the remediation only where both runs wrote `manual`.
MANUAL_KIND = "manual"
# A placeholder evidence object, which `adopt_collector_evidence` replaces
# with the collector's command and excerpt.
PLACEHOLDER = "-"
PLANT_ARGC = 4
# Stamp the planted envelope one week in the past so that a reader of the store
# sees a previous weekly run, not a run that finished seconds ago.
PLANTED_RUN_AGE = timedelta(days=7)
STDERR_TAIL_CHARS = 500


def ensure_proxy_path(proxy_bin: str = CREDENTIAL_PROXY_BIN) -> None:
    """Put the shell sandbox's credential-proxy shims on PATH.

    `kubectl exec` does not start a login shell, so `/etc/profile.d` does not
    add `/opt/credential-proxy/bin` to PATH before `plant.py` runs.
    """
    if not Path(proxy_bin).is_dir():
        return
    current = os.environ.get("PATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    if proxy_bin not in entries:
        os.environ["PATH"] = os.pathsep.join([proxy_bin, *entries])


def load(scripts: str):
    ensure_proxy_path()
    sys.path.insert(0, scripts)
    sys.path.insert(0, str(Path(scripts).parents[2] / "scripts"))
    import audit_report  # noqa: PLC0415 - the module is in the pod, not here

    return audit_report


def planted_finding(audit_report, entry: dict, candidate: dict, mark: str) -> dict:
    check = str(candidate.get("check") or "")
    obj = str(candidate.get("object") or "")
    finding = {
        "check": check,
        "cluster": str(candidate.get("cluster") or entry.get("name") or ""),
        "namespace": str(candidate.get("namespace") or ""),
        "object": obj,
        "severity": candidate.get("severity"),
        "title": f"{mark} {check} {obj}",
        "impact": f"{mark} impact.",
        "recommendation": {
            "action": f"{mark} action.",
            "rationale": f"{mark} rationale.",
            "risk": f"{mark} risk.",
        },
        "remediation": {"kind": MANUAL_KIND, "note": f"{mark} note."},
        "evidence": {"command": PLACEHOLDER, "excerpt": PLACEHOLDER},
    }
    finding["id"] = audit_report.published_id(finding)
    return finding


def plant(audit_report, mark: str, scripts: str) -> int:
    collector = Path(scripts) / COLLECTOR
    run = subprocess.run(
        [sys.executable, str(collector)], capture_output=True, text=True, check=False
    )
    try:
        manifest = json.loads(run.stdout)
    except ValueError:
        tail = run.stderr[-STDERR_TAIL_CHARS:]
        print(f"{COLLECTOR} gave no manifest (exit {run.returncode}): {tail}", file=sys.stderr)
        return 1
    findings = [
        planted_finding(audit_report, entry, candidate, mark)
        for entry, candidate in audit_report._candidates(manifest)
    ]
    audit_report.adopt_collector_evidence(findings, manifest)
    if not findings:
        print(f"{COLLECTOR} gave no candidate; nothing to plant.", file=sys.stderr)
        return 1
    repo = audit_report.resolve_repo(audit_id=AUDIT)
    now = datetime.now(timezone.utc) - PLANTED_RUN_AGE
    envelope = audit_report.report_envelope(
        AUDIT,
        {"status": "UPDATED"},
        {"findings": findings},
        now,
        repo=repo,
        issue_number=None,
        ledger_body="",
        new_ids=[],
        resolved_ids=[],
        rendered_ids=[f["id"] for f in findings],
    )
    audit_report.write_report(AUDIT, envelope, now)
    stored = audit_report.reports_dir_for(AUDIT, repo) / audit_report.REPORT_LATEST_NAME
    try:
        text = stored.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    if mark not in text:
        teardown(audit_report, mark)
        print(f"the store at {stored} does not hold the planted run.", file=sys.stderr)
        return 1
    print(f"planted {len(findings)} finding(s) for {AUDIT} in {repo}.")
    return 0


def teardown(audit_report, mark: str) -> int:
    repo = audit_report.resolve_repo(audit_id=AUDIT)
    directory = audit_report.reports_dir_for(AUDIT, repo)
    candidates = [directory / audit_report.REPORT_LATEST_NAME]
    candidates += sorted((directory / audit_report.REPORT_RUNS_DIR).glob("*.json"))
    removed = 0
    for path in candidates:
        try:
            if mark in path.read_text(encoding="utf-8"):
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    print(f"removed {removed} stored envelope(s) that hold the planted wording.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != PLANT_ARGC:
        print("usage: python3 - plant|teardown <scripts dir> <mark>", file=sys.stderr)
        return 2
    mode, scripts, mark = argv[1:]
    audit_report = load(scripts)
    if mode == "plant":
        return plant(audit_report, mark, scripts)
    if mode == "teardown":
        return teardown(audit_report, mark)
    print(f"unknown mode {mode!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
