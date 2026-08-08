#!/usr/bin/env python3
"""
compute_fleet_audit.py — GCE Compute Engine & MIG Fleet Audit Runner.
"""

import argparse
import json
import os
import subprocess
import sys

def get_target_instances():
    """Retrieves all active running GCE instances in project."""
    host_proj = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GKE_PROJECT_ID")
    cmd = ["gcloud", "compute", "instances", "list", "--format=json"]
    if host_proj:
        cmd.extend(["--project", host_proj])
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0 and res.stdout.strip():
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError:
            pass
    return []

def main():
    parser = argparse.ArgumentParser(description="Audit GCE Compute Engine & MIG Fleet")
    parser.add_argument("--project-id", help="Optional GCP Project ID")
    parser.add_argument("--output", help="Optional path to write findings JSON")
    args = parser.parse_args()

    instances = get_target_instances()
    findings = []
    # Collect compute instance status and boot logs
    for inst in instances:
        name = inst.get("name", "")
        zone = inst.get("zone", "").split("/")[-1]
        pass

    if args.output:
        with open(args.output, "w") as f:
            json.dump(findings, f, indent=2)
        print(f"Wrote {len(findings)} compute findings to {args.output}")
    else:
        print(json.dumps(findings, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
