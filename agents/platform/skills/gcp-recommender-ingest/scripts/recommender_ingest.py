#!/usr/bin/env python3
"""
recommender_ingest.py — GCP Cloud Recommender & GKE Insights Ingest Runner.
Polls Google Cloud Recommender APIs across fleet projects and normalizes recommendations.
"""

import argparse
import json
import os
import subprocess
import sys

RECOMMENDER_SPECS = {
    "iam-least-privilege": {
        "recommender": "google.iam.policy.Recommender",
        "scope_type": "global",
        "severity": "major",
        "remediation_kind": "gcloud"
    },
    "gke-upgrade-available": {
        "recommender": "google.container.DiagnosisRecommender",
        "scope_type": "cluster_location",
        "subtype_filter": "UPGRADE",
        "severity": "major",
        "remediation_kind": "manual"
    },
    "idle-compute-instance": {
        "recommender": "google.compute.instance.IdleResourceRecommender",
        "scope_type": "zone",
        "severity": "minor",
        "remediation_kind": "gcloud"
    },
    "unattached-persistent-disk": {
        "recommender": "google.compute.disk.IdleResourceRecommender",
        "scope_type": "zone",
        "severity": "minor",
        "remediation_kind": "gcloud"
    },
    "gke-webhook-readiness": {
        "recommender": "google.container.DiagnosisRecommender",
        "scope_type": "cluster_location",
        "subtype_filter": "WEBHOOK",
        "severity": "major",
        "remediation_kind": "gcloud"
    },
    "cost-optimization-rightsizing": {
        "recommender": "google.compute.instance.MachineTypeRecommender",
        "scope_type": "zone",
        "severity": "minor",
        "remediation_kind": "gcloud"
    },
    "idle-ip-address": {
        "recommender": "google.compute.address.IdleResourceRecommender",
        "scope_type": "region",
        "severity": "minor",
        "remediation_kind": "gcloud"
    },
    "gke-security-posture-cve": {
        "recommender": "google.container.DiagnosisRecommender",
        "scope_type": "cluster_location",
        "subtype_filter": "SECURITY",
        "severity": "major",
        "remediation_kind": "manual"
    }
}

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

def get_project_locations(project_id: str) -> tuple[list[str], list[str], list[str]]:
    """Resolves active compute zones, regions, and GKE cluster locations for target project."""
    zones = set()
    regions = set()
    cluster_locations = set()

    # Discover zones and regions with active instances or disks
    instances = run_gcloud_json(["gcloud", "compute", "instances", "list", "--project", project_id, "--format=json"])
    if isinstance(instances, list):
        for inst in instances:
            z = inst.get("zone", "").split("/")[-1]
            if z:
                zones.add(z)
                regions.add(z.rsplit("-", 1)[0])

    disks = run_gcloud_json(["gcloud", "compute", "disks", "list", "--project", project_id, "--format=json"])
    if isinstance(disks, list):
        for d in disks:
            z = d.get("zone", "").split("/")[-1]
            if z:
                zones.add(z)
                regions.add(z.rsplit("-", 1)[0])

    # Discover GKE cluster locations
    clusters = run_gcloud_json(["gcloud", "container", "clusters", "list", "--project", project_id, "--format=json"])
    if isinstance(clusters, list):
        for c in clusters:
            loc = c.get("location", "")
            if loc:
                cluster_locations.add(loc)
                if len(loc.split("-")) == 3:
                    zones.add(loc)
                    regions.add(loc.rsplit("-", 1)[0])
                else:
                    regions.add(loc)

    # Fallback to configured compute region/zone if empty
    if not regions:
        rc_r, out_r, _ = run_cmd(["gcloud", "config", "get-value", "compute/region"])
        if rc_r == 0 and out_r.strip():
            regions.add(out_r.strip())
        else:
            regions.add("us-central1")

    if not zones:
        for r in regions:
            zones.add(f"{r}-a")

    return sorted(list(zones)), sorted(list(regions)), sorted(list(cluster_locations))

def query_recommender(project_id: str, recommender_id: str, location: str) -> list[dict]:
    """Queries a specific Cloud Recommender endpoint."""
    cmd = [
        "gcloud", "recommender", "recommendations", "list",
        f"--recommender={recommender_id}",
        f"--location={location}",
        f"--project={project_id}",
        "--format=json"
    ]
    res = run_gcloud_json(cmd)
    return res if isinstance(res, list) else []

def audit_project_recommenders(project_id: str) -> list[dict]:
    """Sweeps all supported Recommenders for target GCP project."""
    findings = []
    zones, regions, cluster_locations = get_project_locations(project_id)

    for check_slug, spec in RECOMMENDER_SPECS.items():
        rec_id = spec["recommender"]
        scope_type = spec["scope_type"]
        severity = spec["severity"]
        rem_kind = spec["remediation_kind"]
        subtype_filter = spec.get("subtype_filter")

        locations = []
        if scope_type == "global":
            locations = ["global"]
        elif scope_type == "zone":
            locations = zones
        elif scope_type == "region":
            locations = regions
        elif scope_type == "cluster_location":
            locations = cluster_locations

        for loc in locations:
            recs = query_recommender(project_id, rec_id, loc)
            for rec in recs:
                rec_name = rec.get("name", "").split("/")[-1]
                rec_subtype = rec.get("recommenderSubtype", "").upper()
                desc = rec.get("description", "")
                
                # If a subtype filter is configured for shared recommenders (like DiagnosisRecommender), enforce it
                if subtype_filter:
                    if subtype_filter not in rec_subtype and subtype_filter not in desc.upper():
                        continue

                state_info = rec.get("stateInfo", {}).get("state", "ACTIVE")
                if state_info != "ACTIVE":
                    continue

                impact = rec.get("primaryImpact", {}).get("category", "COST")
                
                findings.append({
                    "check": check_slug,
                    "severity": severity,
                    "title": f"{desc or rec_id} in {loc}",
                    "cluster": f"project-{project_id}",
                    "namespace": "default",
                    "object": f"Recommender/{rec_name}",
                    "impact": f"Cloud Recommender detected actionable insight ({impact}).",
                    "evidence": {
                        "command": f"gcloud recommender recommendations describe {rec_name} --recommender={rec_id} --location={loc} --project={project_id} --format=json",
                        "excerpt": f"description: {desc[:100]}"
                    },
                    "recommendation": {
                        "action": f"Apply recommendation {rec_name}.",
                        "rationale": desc,
                        "risk": "Verify service dependencies before applying recommendation."
                    },
                    "remediation": {
                        "kind": rem_kind if rem_kind != "manifest" else "gcloud",
                        "path": ""
                    }
                })

    return findings

def main():
    parser = argparse.ArgumentParser(description="Ingest GCP Cloud Recommender & GKE Insights")
    parser.add_argument("--project-id", help="Optional target GCP Project ID")
    parser.add_argument("--output", help="Optional path to write findings JSON")
    args = parser.parse_args()

    target_projects = get_target_projects(args.project_id)
    all_findings = []

    for proj in target_projects:
        proj_findings = audit_project_recommenders(proj)
        all_findings.extend(proj_findings)

    if args.output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w") as f:
                json.dump(all_findings, f, indent=2)
            print(f"Wrote {len(all_findings)} recommender findings across {len(target_projects)} projects to {args.output}")
        except Exception as e:
            sys.stderr.write(f"Failed to write output to {args.output}: {e}\n")
            sys.exit(1)
    else:
        print(f"Collected {len(all_findings)} recommender findings across {len(target_projects)} projects")

if __name__ == "__main__":
    main()
