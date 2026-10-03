#!/usr/bin/env python3
"""
networking_audit.py — GCP VPC Networking Fabric & Routing Audit Helper.
Sweeps Private Service Connect (PSC) forwarding rules across fleet projects.
Additional checks in governance/gcp_networking_fabric_sop.md are evaluated via SOP commands.
"""

import argparse
import json
import os
import re
import subprocess
import sys

MONITORED_PROJECTS_ENV = "MONITORED_PROJECT_IDS"
PROJECT_ENV_VARS = ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID")
GCLOUD = "gcloud"
PROJECTS_LIST_CMD = (GCLOUD, "projects", "list", "--format=value(projectId)")
CONFIG_PROJECT_CMD = (GCLOUD, "config", "get-value", "project")
PSC_REJECTED_STATUSES = ("REJECTED", "CLOSED")
SERVICE_ATTACHMENT_SUBSTR = "serviceAttachments"
AUDIT_SLUG = "gcp-networking-fabric-audit"
PSC_CHECK_SLUG = "psc-routing-deadlock"
PROJECT_TARGET_PREFIX = "project/"
GLOBAL_LOCATION = "global"
UNKNOWN_PROJECT = "unknown"
# The skipped-target name for "every project `gcloud projects list` would have
# named": the listing failed, so how many other projects the fleet holds is
# unknown and the run must read as partial rather than as a full sweep. Same
# name fleet_drift.py uses, so every stream reports it alike.
UNENUMERATED_PROJECTS = "UNENUMERATED_PROJECTS"
# A run narrowed on purpose -- `--project-id` or `MONITORED_PROJECT_IDS` --
# skips discovery, so it reads the named projects and no other. Without a row
# saying so the document reads as the whole fleet, and `finish` resolves every
# ledger finding on a project the run never looked at. fleet_drift.py records
# the same `project/UNENUMERATED_PROJECTS` row for its `--project` runs.
SCOPED_RUN_NOTE = (
    "scope narrowed to {projects} by {source}: discovery was skipped, so no other project "
    "in this fleet was named or read"
)
ERROR_EXCERPT_CHARS = 300
API_DISABLED = "API_DISABLED"
API_DISABLED_MARKERS = (
    "SERVICE_DISABLED",
    "accessNotConfigured",
    "has not been used in project",
)
# A disabled-API refusal names the consumer project -- by number in gcloud's
# usual phrasing, by id in some. With a quota project set, that consumer is the
# quota project rather than `--project`, so the marker alone cannot say whose
# API is off. fleet_waste.refusal_owner draws the same line.
REFUSED_PROJECT_NUMBER_RE = re.compile(r"\bprojects?[ /](\d+)\b")
PROJECT_DESCRIBE_CMD = (GCLOUD, "projects", "describe")
PROJECT_NUMBER_FORMAT = "--format=value(projectNumber)"
PROJECT_FLAG = "--project"
JSON_INDENT = 2


def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    """Runs a shell command and returns (rc, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.returncode, res.stdout, res.stderr
    except Exception as e:
        return -1, "", str(e)


def project_flag_value(cmd: list[str]) -> str | None:
    """The project a gcloud argv names with `--project <id>` or `--project=<id>`."""
    for i, arg in enumerate(cmd):
        if arg == PROJECT_FLAG and i + 1 < len(cmd):
            return cmd[i + 1]
        if arg.startswith(f"{PROJECT_FLAG}="):
            return arg.split("=", 1)[1]
    return None


def refusal_names_project(project: str, stderr: str) -> tuple[bool, str]:
    """Whether an API-disabled refusal is `project`'s own, with why when it is not.

    Only the project's own refusal means it holds nothing to audit. One naming
    another project -- the credential's quota project -- says nothing about this
    one, and read as empty it would drop the project from both `scope.clusters`
    and `scope.skipped`. A refusal naming no project, or one whose number cannot
    be compared with this project's, is a failed read.
    """
    numbers = set(REFUSED_PROJECT_NUMBER_RE.findall(stderr))
    if not numbers:
        if re.search(rf"\b(?i:projects?)[ /]['\"\[]?{re.escape(project)}(?![\w-])", stderr):
            return True, ""
        return False, f"the refusal does not name {project!r}"
    rc, stdout, err = run_cmd([*PROJECT_DESCRIBE_CMD, project, PROJECT_NUMBER_FORMAT])
    if rc != 0:
        return False, (
            f"`gcloud projects describe {project}` failed (rc={rc}), so the refusal's project number "
            f"could not be compared: {(err or '').strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}"
        )
    if numbers == {stdout.strip()}:
        return True, ""
    return False, f"the API is off in a project other than {project!r}, such as a quota project"


def run_gcloud_json(cmd: list[str]) -> list[dict] | dict | str | None:
    """Runs a gcloud command and parses JSON output safely.

    Returns API_DISABLED only for a refusal naming the `--project` the command
    reads; any other failure is None, a failed read.
    """
    rc, stdout, stderr = run_cmd(cmd)
    if rc != 0:
        project = project_flag_value(cmd)
        if project and any(marker in (stderr or "") for marker in API_DISABLED_MARKERS):
            ours, why_not = refusal_names_project(project, stderr)
            if ours:
                return API_DISABLED
            stderr = f"{stderr.strip()} ({why_not})"
        sys.stderr.write(f"gcloud command failed ({rc}): {' '.join(cmd)}\n{stderr}\n")
        return None
    if not stdout.strip():
        return []
    try:
        return json.loads(stdout)
    except Exception as e:
        sys.stderr.write(f"Error parsing gcloud output from {' '.join(cmd)}: {e}\n")
        return None


def get_target_projects(cli_project: str | None = None, listing_errors: list[str] | None = None) -> list[str]:
    """Resolves all target GCP projects to audit.

    Everything that narrows or loses part of the fleet is appended to
    `listing_errors` when the caller passes one, and `main` turns each entry
    into a `project/UNENUMERATED_PROJECTS` skipped target so the run reads as
    partial rather than as the whole fleet:

    - `--project-id` or a non-empty `MONITORED_PROJECT_IDS` narrows the scope
      on purpose and skips discovery;
    - a failed `gcloud projects list` leaves only the env/config project;
    - a listing that succeeds without naming the configured project is
      filtered rather than complete, as fleet_drift.py treats it.

    The host project from `gcloud config get-value project` is always part of
    the discovered scope, alongside any `GCP_PROJECT_ID`-style variable.
    """
    if cli_project and cli_project.strip():
        project = cli_project.strip()
        if listing_errors is not None:
            listing_errors.append(SCOPED_RUN_NOTE.format(projects=project, source="`--project-id`"))
        return [project]

    env_projects = {os.environ.get(var, "").strip() for var in PROJECT_ENV_VARS} - {""}
    # Parsed before it is tested, so a blank or separator-only value reads as
    # unset rather than as an override that names nothing and skips discovery.
    monitored = set(os.environ.get(MONITORED_PROJECTS_ENV, "").replace(",", " ").split())
    if monitored:
        projects = monitored | env_projects
        if listing_errors is not None:
            listing_errors.append(
                SCOPED_RUN_NOTE.format(projects=", ".join(sorted(projects)), source=f"`{MONITORED_PROJECTS_ENV}`")
            )
        return sorted(projects)

    projects = set(env_projects)
    rc, stdout, _ = run_cmd(list(CONFIG_PROJECT_CMD))
    if rc == 0 and stdout.strip():
        projects.add(stdout.strip())

    rc, stdout, stderr = run_cmd(list(PROJECTS_LIST_CMD))
    if rc != 0 and listing_errors is not None:
        listing_errors.append(
            f"`gcloud projects list` rc={rc}: {(stderr or '').strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}; "
            "the scope fell back to the configured project"
        )
    if rc == 0:
        listed = {line.strip() for line in stdout.splitlines() if line.strip()}
        omitted = sorted(projects - listed)
        if omitted and listing_errors is not None:
            listing_errors.append(
                f"`gcloud projects list` rc=0 did not name {', '.join(omitted)}, so the listing is filtered"
            )
        projects |= listed

    return sorted(projects)


def audit_project_networking(project_id: str, skipped_targets: list, active_targets: list) -> list[dict]:
    """Audits PSC forwarding rules in a project (psc-routing-deadlock).

    A project that cannot be read is recorded in skipped_targets and yields no
    findings; the caller keeps going, because one project the agent lacks
    permission on must not decide the outcome for the rest of the fleet.
    """
    findings = []
    target_name = f"{PROJECT_TARGET_PREFIX}{project_id}"

    # 1. Inspect PSC forwarding rules for disconnected / rejected attachments
    list_cmd = f"gcloud compute forwarding-rules list --project={project_id} --format=json"
    fwd_rules = run_gcloud_json(["gcloud", "compute", "forwarding-rules", "list", "--project", project_id, "--format=json"])
    if fwd_rules == API_DISABLED:
        return findings
    if fwd_rules is None or not isinstance(fwd_rules, list):
        skipped_targets.append({
            "cluster": target_name,
            "name": target_name,
            "location": GLOBAL_LOCATION,
            "project": project_id,
            "reason": f"Failed to list forwarding rules in project {project_id} (permission denied or API unavailable)"
        })
        return findings

    active_targets.append({
        "name": target_name,
        "location": GLOBAL_LOCATION,
        "project": project_id,
        "checks_run": [{"check": PSC_CHECK_SLUG, "command": list_cmd}]
    })

    for fr in fwd_rules:
        name = fr.get("name", "")
        region = fr.get("region", "").split("/")[-1]
        target = fr.get("target", "")
        psc_status = fr.get("pscConnectionStatus", "")
        
        # Only flag when target is a service attachment AND the status is rejected or closed
        if target and SERVICE_ATTACHMENT_SUBSTR in target and psc_status in PSC_REJECTED_STATUSES:
            findings.append({
                "check": PSC_CHECK_SLUG,
                "severity": "major",
                "title": f"Private Service Connect forwarding rule {name} in {region} is in state {psc_status}",
                "cluster": target_name,
                "namespace": "",
                "object": f"ForwardingRule/{name}",
                "impact": f"PSC endpoint {name} cannot route traffic to target service attachment.",
                "evidence": {
                    "command": f"gcloud compute forwarding-rules describe {name} --region={region} --project={project_id} --format=json",
                    "excerpt": f"pscConnectionStatus: {psc_status}"
                },
                "recommendation": {
                    "action": f"Re-establish or re-authorize PSC service attachment connection for {name}.",
                    "rationale": "Service attachment rejected or closed the connection request.",
                    "risk": "Requires verifying target service consumer acceptance list."
                },
                "remediation": {
                    "kind": "gcloud",
                    "path": "",
                    "note": f"gcloud compute forwarding-rules describe {name} --region={region} --project={project_id}"
                }
            })

    return findings

def main():
    parser = argparse.ArgumentParser(description="Audit GCP VPC Networking Fabric")
    parser.add_argument("--project-id", help="Optional GCP Project ID")
    parser.add_argument("--output", help="Optional path to write findings JSON")
    args = parser.parse_args()

    listing_errors: list[str] = []
    target_projects = get_target_projects(args.project_id, listing_errors)
    all_findings = []
    skipped_targets = []
    active_targets = []

    for error in listing_errors:
        sys.stderr.write(f"{error}; auditing {target_projects or 'no project'}\n")
        skipped_targets.append({
            "cluster": f"{PROJECT_TARGET_PREFIX}{UNENUMERATED_PROJECTS}",
            "name": f"{PROJECT_TARGET_PREFIX}{UNENUMERATED_PROJECTS}",
            "location": GLOBAL_LOCATION,
            "project": UNENUMERATED_PROJECTS,
            "reason": f"{error}. How many other projects the fleet holds is unknown.",
        })

    if not target_projects:
        sys.stderr.write("No target projects resolved from CLI, environment, or gcloud.\n")
        skipped_targets.append({
            "cluster": f"{PROJECT_TARGET_PREFIX}{UNKNOWN_PROJECT}",
            "name": f"{PROJECT_TARGET_PREFIX}{UNKNOWN_PROJECT}",
            "location": GLOBAL_LOCATION,
            "project": UNKNOWN_PROJECT,
            "reason": "No GCP project ID configured or resolved"
        })

    for proj in target_projects:
        all_findings.extend(audit_project_networking(proj, skipped_targets, active_targets))

    findings_document = {
        "audit": AUDIT_SLUG,
        "scope": {
            "clusters": active_targets,
            "skipped": skipped_targets
        },
        "findings": all_findings
    }

    if args.output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(findings_document, f, indent=JSON_INDENT)
        except Exception as e:
            sys.stderr.write(f"Failed to write output to {args.output}: {e}\n")
            sys.exit(1)

    written = f"; wrote {args.output}" if args.output else "; no --output, nothing written"
    print(f"Found {len(all_findings)} networking findings across {len(active_targets)} active projects. "
          f"{len(skipped_targets)} targets skipped{written}.")

if __name__ == "__main__":
    main()
