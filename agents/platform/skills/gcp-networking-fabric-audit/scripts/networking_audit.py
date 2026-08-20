#!/usr/bin/env python3
"""
networking_audit.py — GCP VPC Networking Fabric & Routing Audit Helper.
Sweeps Private Service Connect (PSC) forwarding rules across fleet projects.
Additional checks in governance/gcp_networking_fabric_sop.md are evaluated via SOP commands.
"""

import argparse
import json
import os
import subprocess
import sys

def run_cmd(cmd: list[str]) -> tuple[int, str, str]:
    """Runs a shell command and returns (rc, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return res.returncode, res.stdout, res.stderr
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
        for p in monitored.split(","):
            p = p.strip()
            if p:
                projects.add(p)

    for env_var in ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID"):
        val = os.environ.get(env_var, "").strip()
        if val:
            projects.add(val)

    if not projects:
        rc, stdout, _ = run_cmd(["gcloud", "config", "get-value", "project"])
        if rc == 0 and stdout.strip():
            projects.add(stdout.strip())

    return sorted(list(projects))

def audit_project_networking(project_id: str) -> list[dict]:
    """Audits PSC forwarding rules in a project (psc-routing-deadlock)."""
    findings = []
    
    # 1. Inspect PSC forwarding rules for disconnected / rejected attachments
    fwd_rules = run_gcloud_json(["gcloud", "compute", "forwarding-rules", "list", "--project", project_id, "--format=json"])
    if fwd_rules is None:
        return None
        
    for fr in fwd_rules:
        name = fr.get("name", "")
        region = fr.get("region", "").split("/")[-1]
        target = fr.get("target", "")
        psc_status = fr.get("pscConnectionStatus", "")
        
        # Only flag when target is a service attachment AND the status is rejected or closed
        if target and "serviceAttachments" in target and psc_status in ("REJECTED", "CLOSED"):
            findings.append({
                "check": "psc-routing-deadlock",
                "severity": "major",
                "title": f"Private Service Connect forwarding rule {name} in {region} is in state {psc_status}",
                "cluster": f"project/{project_id}",
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
                    "path": ""
                }
            })

    return findings

def main():
    parser = argparse.ArgumentParser(description="Audit GCP VPC Networking Fabric")
    parser.add_argument("--project-id", help="Optional GCP Project ID")
    parser.add_argument("--output", help="Optional path to write findings JSON")
    args = parser.parse_args()

    target_projects = get_target_projects(args.project_id)
    all_findings = []

    for proj in target_projects:
        proj_findings = audit_project_networking(proj)
        if proj_findings is None:
            sys.stderr.write(f"Error: Failed to query networking info for project {proj}\n")
            sys.exit(1)
        all_findings.extend(proj_findings)

    if args.output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w") as f:
                json.dump(all_findings, f, indent=2)
            print(f"Wrote {len(all_findings)} networking findings across {len(target_projects)} projects to {args.output}")
        except Exception as e:
            sys.stderr.write(f"Failed to write output to {args.output}: {e}\n")
            sys.exit(1)
    else:
        print(f"Collected {len(all_findings)} networking findings across {len(target_projects)} projects")

if __name__ == "__main__":
    main()
