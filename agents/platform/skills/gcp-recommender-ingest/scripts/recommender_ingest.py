#!/usr/bin/env python3
"""
recommender_ingest.py — Ingests GCP Recommender signals and GKE notifications into fleet audit findings.
"""

import argparse
import json
import os
import subprocess
import sys

RECOMMENDERS = [
    ("iam-least-privilege", "google.iam.policy.Recommender", "global"),
    ("idle-compute-instance", "google.compute.instance.IdleResourceRecommendation", "us-central1-a"),
    ("unattached-persistent-disk", "google.compute.disk.IdleResourceRecommendation", "us-central1-a"),
    ("cost-optimization-rightsizing", "google.container.CostOptimizationRecommendation", "us-central1-a"),
]

def get_fleet_projects():
    """Resolves all monitored fleet GCP projects from environment or gcloud config."""
    host_proj = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GKE_PROJECT_ID")
    if not host_proj:
        res = subprocess.run(["gcloud", "config", "get-value", "project"], capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            host_proj = res.stdout.strip()
        else:
            host_proj = "default"

    monitored_raw = os.environ.get("MONITORED_PROJECT_IDS", "")
    projects = [host_proj]
    if monitored_raw:
        for p in monitored_raw.split(","):
            p = p.strip()
            if p and p not in projects:
                projects.append(p)
    return projects

def query_recommenders():
    """Queries GCP Recommender API across all fleet projects."""
    projects = get_fleet_projects()
    all_findings = []

    for proj in projects:
        for check_slug, rec_name, loc in RECOMMENDERS:
            cmd = [
                "gcloud", "recommender", "recommendations", "list",
                f"--recommender={rec_name}",
                f"--project={proj}",
                f"--location={loc}",
                "--format=json"
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip() and res.stdout.strip() != "[]":
                try:
                    data = json.loads(res.stdout)
                    for item in data:
                        rec_id = item.get("name", "").split("/")[-1]
                        desc = item.get("description", "GCP Recommender recommendation")
                        all_findings.append({
                            "check": check_slug,
                            "project": proj,
                            "id": rec_id,
                            "description": desc,
                            "severity": "major",
                            "evidence": {
                                "command": " ".join(cmd),
                                "excerpt": desc
                            }
                        })
                except json.JSONDecodeError as err:
                    print(f"Warning: Failed to parse JSON from {rec_name}: {err}", file=sys.stderr)

    return all_findings

def main():
    parser = argparse.ArgumentParser(description="Ingest GCP Recommender Insights")
    parser.add_argument("--output", help="Optional path to write findings JSON")
    args = parser.parse_args()

    findings = query_recommenders()
    if args.output:
        with open(args.output, "w") as f:
            json.dump(findings, f, indent=2)
        print(f"Wrote {len(findings)} findings to {args.output}")
    else:
        print(json.dumps(findings, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main())
