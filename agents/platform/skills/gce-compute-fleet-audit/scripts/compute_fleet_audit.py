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

def run_cmd(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """Runs a shell command with a timeout and returns (rc, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout} seconds"
    except Exception as e:
        return -1, "", str(e)

def run_gcloud_json(cmd: list[str]) -> list[dict] | dict | None:
    """Runs a gcloud command and parses JSON output safely."""
    rc, stdout, stderr = run_cmd(cmd)
    if rc != 0:
        sys.stderr.write(f"gcloud command failed ({rc}): {' '.join(cmd)}\n{stderr}\n")
        return None
    if not stdout.strip():
        return []
    try:
        return json.loads(stdout)
    except Exception as e:
        sys.stderr.write(f"Error parsing gcloud output from {' '.join(cmd)}: {e}\n")
        return None

def get_target_projects(cli_project: str | None = None) -> list[str]:
    """Resolves all target GCP projects to audit."""
    if cli_project:
        return [cli_project]

    projects = set()
    monitored = os.environ.get("MONITORED_PROJECT_IDS", "")
    if monitored:
        for p in monitored.replace(",", " ").split():
            p = p.strip()
            if p:
                projects.add(p)

    if not projects:
        rc, stdout, _ = run_cmd(["gcloud", "projects", "list", "--format=value(projectId)"])
        if rc == 0 and stdout.strip():
            for line in stdout.strip().splitlines():
                if line.strip():
                    projects.add(line.strip())

    if not projects:
        for env_var in ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID"):
            val = os.environ.get(env_var, "").strip()
            if val:
                projects.add(val)

    if not projects:
        rc, stdout, _ = run_cmd(["gcloud", "config", "get-value", "project"])
        if rc == 0 and stdout.strip():
            projects.add(stdout.strip())

    return sorted(list(projects))

def audit_project_compute(project_id: str) -> list[dict]:
    """Audits instances and snapshots in target project."""
    findings = []

    # 1. Inspect running compute instances for startup script errors in serial port output
    instances = run_gcloud_json(["gcloud", "compute", "instances", "list", "--project", project_id, "--format=json"])
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
                        "cluster": f"project-{project_id}",
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
                            "cluster": f"project-{project_id}",
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

    return findings

def main():
    parser = argparse.ArgumentParser(description="Audit GCE Compute Engine & MIG Fleet")
    parser.add_argument("--project-id", help="Optional GCP Project ID")
    parser.add_argument("--output", help="Optional path to write findings JSON")
    args = parser.parse_args()

    target_projects = get_target_projects(args.project_id)
    all_findings = []

    for proj in target_projects:
        proj_findings = audit_project_compute(proj)
        all_findings.extend(proj_findings)

    if args.output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w") as f:
                json.dump(all_findings, f, indent=2)
        except Exception as e:
            sys.stderr.write(f"Failed to write output to {args.output}: {e}\n")

    print(f"Wrote {len(all_findings)} compute findings across {len(target_projects)} projects")

if __name__ == "__main__":
    main()
