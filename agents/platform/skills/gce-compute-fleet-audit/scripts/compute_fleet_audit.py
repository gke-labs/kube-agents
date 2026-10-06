#!/usr/bin/env python3
"""
compute_fleet_audit.py — GCE Compute Engine & MIG Fleet Audit Runner.
Sweeps GCP projects for startup script failures and orphaned storage snapshots.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

MONITORED_PROJECTS_ENV = "MONITORED_PROJECT_IDS"
PROJECT_ENV_VARS = ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID")
GCLOUD = "gcloud"
PROJECT_ID_FORMAT = "--format=value(projectId)"
PROJECTS_LIST_CMD = (GCLOUD, "projects", "list", PROJECT_ID_FORMAT)
CONFIG_PROJECT_CMD = (GCLOUD, "config", "get-value", "project")
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


def run_cmd(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Runs a shell command with a timeout and returns (rc, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout} seconds"
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


def run_gcloud_json(cmd: list[str]) -> tuple[list[dict] | dict | str | None, str | None]:
    """Runs a gcloud command and parses JSON output safely.

    Returns (API_DISABLED, None) only for a refusal naming the `--project` the
    command reads; any other failure returns (None, error_message).
    """
    rc, stdout, stderr = run_cmd(cmd)
    if rc != 0:
        project = project_flag_value(cmd)
        if project and any(marker in (stderr or "") for marker in API_DISABLED_MARKERS):
            ours, why_not = refusal_names_project(project, stderr)
            if ours:
                return API_DISABLED, None
            stderr = f"{stderr.strip()} ({why_not})"
        sys.stderr.write(f"gcloud command failed ({rc}): {' '.join(cmd)}\n{stderr}\n")
        return None, f"{' '.join(cmd)} failed ({rc}): {stderr.strip()}"
    if not stdout.strip():
        return [], None
    try:
        return json.loads(stdout), None
    except Exception as e:
        sys.stderr.write(f"Error parsing gcloud output from {' '.join(cmd)}: {e}\n")
        return None, f"{' '.join(cmd)} returned unparsable JSON: {e}"


def _normalise_project_id(project: str, listing_errors: list[str] | None = None) -> str | None:
    """Resolves a numeric project number (e.g. from spec.harness.projectId) to its projectId."""
    if not project.isdigit():
        return project
    rc, stdout, stderr = run_cmd([*PROJECT_DESCRIBE_CMD, project, PROJECT_ID_FORMAT])
    if rc == 0 and stdout.strip():
        return stdout.strip()
    if listing_errors is not None:
        listing_errors.append(
            f"`gcloud projects describe {project}` rc={rc}: "
            f"{(stderr or '').strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}; "
            "numeric project could not be resolved to a projectId"
        )
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
        raw = cli_project.strip()
        project = _normalise_project_id(raw) or raw
        if listing_errors is not None:
            listing_errors.append(SCOPED_RUN_NOTE.format(projects=project, source="`--project-id`"))
        return [project]

    env_projects = {os.environ.get(var, "").strip() for var in PROJECT_ENV_VARS} - {""}
    # Parsed before it is tested, so a blank or separator-only value reads as
    # unset rather than as an override that names nothing and skips discovery.
    monitored = set(os.environ.get(MONITORED_PROJECTS_ENV, "").replace(",", " ").split())
    if monitored:
        projects = {_normalise_project_id(p) or p for p in monitored | env_projects}
        if listing_errors is not None:
            listing_errors.append(
                SCOPED_RUN_NOTE.format(projects=", ".join(sorted(projects)), source=f"`{MONITORED_PROJECTS_ENV}`")
            )
        return sorted(projects)

    raw_projects = set(env_projects)
    rc, stdout, _ = run_cmd(list(CONFIG_PROJECT_CMD))
    if rc == 0 and stdout.strip():
        raw_projects.add(stdout.strip())
    projects = {
        resolved
        for p in sorted(raw_projects)
        if (resolved := _normalise_project_id(p, listing_errors)) is not None
    }

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

    return sorted(projects or raw_projects)


def audit_project_compute(project_id: str, skipped_targets: list, active_targets: list) -> list[dict]:
    """Audits instances and snapshots in target project."""
    findings = []
    checks_run = []
    limitations = []
    target_name = f"{PROJECT_TARGET_PREFIX}{project_id}"

    # 1. Inspect running compute instances for startup script errors in serial port output
    instances, error = run_gcloud_json(["gcloud", "compute", "instances", "list", "--project", project_id, "--format=json"])
    if instances == API_DISABLED:
        return findings
    if error is not None or not isinstance(instances, list):
        skipped_targets.append({
            "cluster": target_name,
            "name": target_name,
            "location": GLOBAL_LOCATION,
            "project": project_id,
            "reason": error or f"Failed to list compute instances in project {project_id}"
        })
        return findings

    checks_run.append({
        "check": "gce-startup-script-status",
        "command": f"gcloud compute instances list --project={project_id} --format=json"
    })

    if isinstance(instances, list):
        for inst in instances:
            name = inst.get("name", "")
            zone = inst.get("zone", "").split("/")[-1]
            status = inst.get("status", "")
            if status != "RUNNING" or not name or not zone:
                continue

            # Check serial port output for startup script failure
            cmd = ["gcloud", "compute", "instances", "get-serial-port-output", name, f"--zone={zone}", f"--project={project_id}"]
            rc_serial, serial_out, _ = run_cmd(cmd)
            if rc_serial == 0 and serial_out:
                matched_line = ""
                for line in serial_out.splitlines():
                    if "startup-script exit status 1" in line or "Finished running startup scripts with error" in line:
                        matched_line = line.strip()
                        break

                if matched_line:
                    findings.append({
                        "check": "gce-startup-script-status",
                        "severity": "critical",
                        "title": f"Compute instance {name} in {zone} startup script failed with error",
                        "cluster": target_name,
                        "namespace": "",
                        "object": f"ComputeInstance/{name}",
                        "impact": f"Instance {name} failed initialization and may be in a degraded or unbootstrapped state.",
                        "evidence": {
                            "command": f"gcloud compute instances get-serial-port-output {name} --zone={zone} --project={project_id}",
                            "excerpt": matched_line
                        },
                        "recommendation": {
                            "action": f"Inspect serial port logs for instance {name} and resolve startup script failure.",
                            "rationale": "Startup script encountered non-zero return code during VM initialization.",
                            "risk": "VM restart may be required after updating metadata startup scripts."
                        },
                        "remediation": {
                            "kind": "gcloud",
                            "path": "",
                            "note": f"gcloud compute instances reset {name} --zone={zone} --project={project_id}"
                        }
                    })

    # 2. Inspect disks and snapshots for orphaned snapshots of deleted source disks
    disks, _ = run_gcloud_json(["gcloud", "compute", "disks", "list", "--project", project_id, "--format=json"])
    if disks is not None and isinstance(disks, list):
        active_disk_names = set()
        for d in disks:
            d_name = d.get("name", "")
            self_link = d.get("selfLink", "")
            if d_name:
                active_disk_names.add(d_name)
            if self_link:
                active_disk_names.add(self_link)

        snapshots, _ = run_gcloud_json(["gcloud", "compute", "snapshots", "list", "--project", project_id, "--format=json"])
        if isinstance(snapshots, list):
            checks_run.append({
                "check": "orphaned-snapshots",
                "command": f"gcloud compute snapshots list --project={project_id} --format=json"
            })
            now = datetime.datetime.now(datetime.timezone.utc)
            for snap in snapshots:
                s_name = snap.get("name", "")
                source_disk = snap.get("sourceDisk", "")
                source_disk_name = source_disk.split("/")[-1] if source_disk else ""
                creation_timestamp_str = snap.get("creationTimestamp", "")
                resource_policies = snap.get("resourcePolicies", [])

                # Must have a source disk reference that is no longer in active disks and no active backup policy
                if source_disk_name and source_disk_name not in active_disk_names and source_disk not in active_disk_names and not resource_policies:
                    is_old = False
                    if creation_timestamp_str:
                        try:
                            ts = datetime.datetime.fromisoformat(creation_timestamp_str.replace("Z", "+00:00"))
                            if (now - ts).days > 90:
                                is_old = True
                        except Exception:
                            pass

                    if is_old:
                        findings.append({
                            "check": "orphaned-snapshots",
                            "severity": "minor",
                            "title": f"Orphaned snapshot {s_name} retained from deleted disk {source_disk_name}",
                            "cluster": target_name,
                            "namespace": "",
                            "object": f"Snapshot/{s_name}",
                            "impact": f"Snapshot {s_name} incurs ongoing storage charges without active source disk.",
                            "evidence": {
                                "command": f"gcloud compute snapshots list --project={project_id} --format=json",
                                "excerpt": f'{{"name": "{s_name}", "sourceDisk": "{source_disk_name}", "creationTimestamp": "{creation_timestamp_str}"}}'
                            },
                            "recommendation": {
                                "action": f"Clean up orphaned storage snapshot {s_name}.",
                                "rationale": "Source disk has been deleted and snapshot is unattached for > 90 days.",
                                "risk": "Ensure no disaster recovery archive requirements exist."
                            },
                            "remediation": {
                                "kind": "gcloud",
                                "path": "",
                                "note": f"gcloud compute snapshots delete {s_name} --project={project_id} --quiet"
                            }
                        })

    target_scope = {
        "name": target_name,
        "location": "global",
        "project": project_id,
        "checks_run": checks_run
    }
    if limitations:
        target_scope["limitations"] = "; ".join(limitations)
    active_targets.append(target_scope)

    return findings

def main():
    parser = argparse.ArgumentParser(description="Audit GCE Compute Engine & MIG Fleet")
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
        proj_findings = audit_project_compute(proj, skipped_targets, active_targets)
        all_findings.extend(proj_findings)

    findings_document = {
        "audit": "gce-compute-fleet-audit",
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
                json.dump(findings_document, f, indent=2)
        except Exception as e:
            sys.stderr.write(f"Failed to write output to {args.output}: {e}\n")
            sys.exit(1)

    written = f"; wrote {args.output}" if args.output else "; no --output, nothing written"
    print(f"Found {len(all_findings)} compute findings across {len(active_targets)} active projects. "
          f"{len(skipped_targets)} targets skipped{written}.")

if __name__ == "__main__":
    main()
