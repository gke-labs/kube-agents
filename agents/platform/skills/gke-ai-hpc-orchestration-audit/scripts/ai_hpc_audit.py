#!/usr/bin/env python3
"""
ai_hpc_audit.py — GKE AI/ML & HPC Workload Orchestration Audit Runner.
Sweeps accelerator clusters for Kueue cluster queues lacking borrowing limits.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile

def run_cmd(cmd: list[str], env: dict | None = None) -> tuple[int, str, str]:
    """Runs a shell command and returns (rc, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
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
        sys.stderr.write(f"Error parsing JSON from {' '.join(cmd)}: {e}\n")
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

def inspect_cluster_accelerators(project_id: str, cluster_name: str, location: str) -> list[dict]:
    """Inspects Kueue and accelerator configuration using isolated kubeconfig."""
    findings = []
    
    kc_dir = os.path.expanduser(f"{os.environ.get('HERMES_HOME', '/opt/data')}/.kubeconfigs")
    os.makedirs(kc_dir, exist_ok=True)
    kc_path = f"{kc_dir}/kubeconfig_{project_id}_{cluster_name}_{location}.yaml"

    try:
        env = os.environ.copy()
        env["KUBECONFIG"] = kc_path

        rc, _, stderr = run_cmd([
            "gcloud", "container", "clusters", "get-credentials", cluster_name,
            f"--location={location}", f"--project={project_id}"
        ], env=env)

        if rc != 0:
            sys.stderr.write(f"Could not get credentials for {cluster_name}: {stderr}\n")
            return findings

        # Check ClusterQueues for cohort borrowing limits
        rc, stdout, _ = run_cmd(["kubectl", "get", "clusterqueues", "-o", "json"], env=env)
        if rc == 0 and stdout.strip():
            try:
                cqs = json.loads(stdout).get("items", [])
                for cq in cqs:
                    name = cq.get("metadata", {}).get("name", "")
                    spec = cq.get("spec", {})
                    cohort = spec.get("cohort", "")
                    if not cohort:
                        continue

                    # Check resource groups for missing borrowing limits per resource
                    unbounded_resources = []
                    for rg in spec.get("resourceGroups", []):
                        for fl in rg.get("flavors", []):
                            for r in fl.get("resources", []):
                                r_name = r.get("name", "unknown")
                                if "borrowingLimit" not in r:
                                    unbounded_resources.append(r_name)

                    if unbounded_resources:
                        unbounded_str = ", ".join(sorted(set(unbounded_resources)))
                        findings.append({
                            "check": "kueue-cohort-starvation",
                            "severity": "major",
                            "title": f"ClusterQueue {name} in cohort {cohort} lacks explicit borrowing limit for {unbounded_str}",
                            "cluster": cluster_name,
                            "namespace": "",
                            "object": f"ClusterQueue/{name}",
                            "impact": f"Workloads in shared cohort can starve high-priority queues during peak batch bursts on {unbounded_str}.",
                            "evidence": {
                                "command": f"kubectl get clusterqueue {name} -o json",
                                "excerpt": f"cohort: {cohort}, unboundedResources: {unbounded_str}"
                            },
                            "recommendation": {
                                "action": f"Set explicit borrowingLimit in ClusterQueue {name} spec.",
                                "rationale": "Prevents cohort resource exhaustion across tenant queues.",
                                "risk": "Limits maximum opportunistic burst capacity."
                            }
                        })
            except Exception as e:
                sys.stderr.write(f"Error checking ClusterQueues on {cluster_name}: {e}\n")
    finally:
        pass

    return findings

def audit_project_accelerators(project_id: str) -> list[dict]:
    """Audits accelerator clusters in target project."""
    findings = []
    clusters = run_gcloud_json(["gcloud", "container", "clusters", "list", "--project", project_id, "--format=json"])
    if not isinstance(clusters, list):
        return findings

    for c in clusters:
        name = c.get("name", "")
        loc = c.get("location", "")
        status = c.get("status", "")
        if status != "RUNNING" or not name:
            continue

        c_findings = inspect_cluster_accelerators(project_id, name, loc)
        findings.extend(c_findings)

    return findings

def main():
    parser = argparse.ArgumentParser(description="Audit GKE AI/ML and HPC Workloads")
    parser.add_argument("--project-id", help="Optional target GCP Project ID")
    parser.add_argument("--output", help="Optional path to write findings JSON")
    args = parser.parse_args()

    target_projects = get_target_projects(args.project_id)
    all_findings = []

    for proj in target_projects:
        proj_findings = audit_project_accelerators(proj)
        all_findings.extend(proj_findings)

    if args.output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w") as f:
                json.dump(all_findings, f, indent=2)
        except Exception as e:
            sys.stderr.write(f"Failed to write output to {args.output}: {e}\n")

    print(f"Wrote {len(all_findings)} AI/HPC findings across {len(target_projects)} projects")

if __name__ == "__main__":
    main()
