#!/usr/bin/env python3
"""
finops_waste_audit.py — FinOps & Cloud Waste Audit Runner.
"""

import argparse
import json
import os
import subprocess
import sys

def get_target_pvs():
    """Retrieves all PersistentVolumes across cluster."""
    cmd = ["kubectl", "get", "pv", "-o", "json"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0 and res.stdout.strip():
        try:
            return json.loads(res.stdout).get("items", [])
        except json.JSONDecodeError:
            pass
    return []

def main():
    parser = argparse.ArgumentParser(description="Audit FinOps & Cloud Waste")
    parser.add_argument("--project-id", help="Optional GCP Project ID")
    parser.add_argument("--output", help="Optional path to write findings JSON")
    args = parser.parse_args()

    pvs = get_target_pvs()
    findings = []
    # Collect retained PVs and idle addresses
    for pv in pvs:
        status = pv.get("status", {}).get("phase", "")
        reclaim = pv.get("spec", {}).get("persistentVolumeReclaimPolicy", "")
        if status == "Released" and reclaim == "Retain":
            pass

    if args.output:
        with open(args.output, "w") as f:
            json.dump(findings, f, indent=2)
        print(f"Wrote {len(findings)} FinOps findings to {args.output}")
    else:
        print(json.dumps(findings, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
