#!/usr/bin/env python3
"""
compute_fleet_audit.py — GCE Compute Engine & MIG Fleet Audit Runner.
Sweeps GCP projects for startup script failures and orphaned storage snapshots.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

MONITORED_PROJECTS_ENV = "MONITORED_PROJECT_IDS"
PROJECT_ENV_VARS = ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID")
GCLOUD = "gcloud"
PROJECTS_LIST_CMD = (GCLOUD, "projects", "list", "--format=value(projectId)")
CONFIG_PROJECT_CMD = (GCLOUD, "config", "get-value", "project")
DEFAULT_TIMEOUT_SECONDS = 60
ORPHANED_SNAPSHOT_AGE_DAYS = 90
PROJECT_TARGET_PREFIX = "project/"
GLOBAL_LOCATION = "global"
# The skipped-target name for "every project `gcloud projects list` would have
# named": the listing failed, so how many other projects the fleet holds is
# unknown and the run must read as partial rather than as a full sweep. Same
# name fleet_drift.py uses, so every stream reports it alike.
UNENUMERATED_PROJECTS = "UNENUMERATED_PROJECTS"
ERROR_EXCERPT_CHARS = 300
API_DISABLED = "API_DISABLED"
API_DISABLED_MARKERS = (
    "SERVICE_DISABLED",
    "accessNotConfigured",
    "has not been used in project",
)
JSON_INDENT = 2


def run_cmd(cmd: list[str], timeout: int = DEFAULT_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Runs a shell command with a timeout and returns (rc, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout} seconds"
    except Exception as e:
        return -1, "", str(e)


def run_gcloud_json(cmd: list[str]) -> list[dict] | dict | str | None:
    """Runs a gcloud command and parses JSON output safely."""
    rc, stdout, stderr = run_cmd(cmd)
    if rc != 0:
        if any(marker in (stderr or "") for marker in API_DISABLED_MARKERS):
            return API_DISABLED
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

    A failed `gcloud projects list` is appended to `listing_errors` when the
    caller passes one: the scope then holds only the env/config project, and a
    caller that dropped the error would publish that narrowed sweep as the
    whole fleet. A listing that succeeds without naming the configured project
    is appended too: it is filtered rather than complete, as fleet_drift.py
    treats it.
    """
    if cli_project:
        return [cli_project.strip()]

    projects = set()
    monitored = os.environ.get(MONITORED_PROJECTS_ENV, "")
    if monitored:
        for p in monitored.replace(",", " ").split():
            p = p.strip()
            if p:
                projects.add(p)

    for env_var in PROJECT_ENV_VARS:
        val = os.environ.get(env_var, "").strip()
        if val:
            projects.add(val)

    if not projects:
        rc, stdout, _ = run_cmd(list(CONFIG_PROJECT_CMD))
        if rc == 0 and stdout.strip():
            projects.add(stdout.strip())

    if not monitored:
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

    return sorted(list(projects))

def audit_project_compute(project_id: str, skipped_targets: list, active_targets: list) -> list[dict]:
    """Audits instances and snapshots in target project."""
    findings = []
    checks_run = []
    limitations = []
    target_name = f"{PROJECT_TARGET_PREFIX}{project_id}"

    # 1. Inspect running compute instances for startup script errors in serial port output
    instances = run_gcloud_json(["gcloud", "compute", "instances", "list", "--project", project_id, "--format=json"])
    if instances == API_DISABLED:
        return findings
    if instances is None or not isinstance(instances, list):
        skipped_targets.append({
            "cluster": target_name,
            "name": target_name,
            "location": "global",
            "project": project_id,
            "reason": f"Failed to list compute instances in project {project_id} (permission denied or API unavailable)"
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
    disks = run_gcloud_json(["gcloud", "compute", "disks", "list", "--project", project_id, "--format=json"])
    if disks is not None and isinstance(disks, list):
        active_disk_names = set()
        for d in disks:
            d_name = d.get("name", "")
            self_link = d.get("selfLink", "")
            if d_name:
                active_disk_names.add(d_name)
            if self_link:
                active_disk_names.add(self_link)

        snapshots = run_gcloud_json(["gcloud", "compute", "snapshots", "list", "--project", project_id, "--format=json"])
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
                            if (now - ts).days > ORPHANED_SNAPSHOT_AGE_DAYS:
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
        sys.stderr.write(f"{error}; scope fell back to {target_projects or 'no project'}\n")
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
            "cluster": f"{PROJECT_TARGET_PREFIX}unknown",
            "name": f"{PROJECT_TARGET_PREFIX}unknown",
            "location": "global",
            "project": "unknown",
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
                json.dump(findings_document, f, indent=JSON_INDENT)
        except Exception as e:
            sys.stderr.write(f"Failed to write output to {args.output}: {e}\n")
            sys.exit(1)

    print(f"Wrote {len(all_findings)} compute findings across {len(active_targets)} active projects. {len(skipped_targets)} targets skipped.")

if __name__ == "__main__":
    main()
