#!/usr/bin/env python3
# operator_agent_provision.py - Dynamic GKE IPAM, cluster resource provisioner, and state manager.
# Scans GCP project for GKE CIDRs, validates GKE locations, applies cluster manifest, and updates JSONL state.

import json
import sys
import os
import subprocess
import ipaddress
from pathlib import Path
from datetime import datetime

def log(msg: str):
    print(f"[IPAM-PROVISIONER] {msg}", file=sys.stderr)

def get_hermes_home() -> Path:
    """Return the active HERMES_HOME directory."""
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))

def get_state_file() -> Path:
    """Return the path to the operator agents JSONL file."""
    return get_hermes_home() / "operator_agents.jsonl"

def add_agent_to_state(agent_id: str, cluster_name: str, location: str, project_id: str):
    """Append a new agent entry to the JSONL state file."""
    state_file = get_state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "agent_id": agent_id,
        "cluster_name": cluster_name,
        "location": location,
        "project_id": project_id,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "status": "active",
        "endpoint": f"{agent_id}.agent-system.svc.clusterset.local:8642"
    }

    log(f"Saving new agent '{agent_id}' to state file: {state_file}")
    try:
        with open(state_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        log("State file updated successfully.")
    except Exception as e:
        log(f"Error: Failed to write to state file: {e}")

def get_project_id() -> str:
    """Resolve Project ID from USER.md or gcloud config."""
    user_md = Path("USER.md")
    if user_md.exists():
        try:
            content = user_md.read_text(encoding="utf-8")
            for line in content.splitlines():
                if "project:" in line.lower():
                    val = line.split(":", 1)[1].strip().strip('"').strip("'")
                    if val:
                        log(f"Resolved Project ID from USER.md: {val}")
                        return val
        except Exception as e:
            log(f"Warning: Failed to parse USER.md: {e}")

    # Fallback to gcloud config
    try:
        res = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True, text=True, check=True
        )
        val = res.stdout.strip()
        if val and val != "(unset)":
            log(f"Resolved Project ID from gcloud: {val}")
            return val
    except Exception as e:
        log(f"Warning: Failed to query gcloud config: {e}")

    return ""

def get_valid_regions(project_id: str) -> list[str]:
    """Retrieve the live list of enabled Google Cloud regions for the GKE API."""
    try:
        log("Querying Google Cloud for valid regions...")
        res = subprocess.run(
            [
                "gcloud", "compute", "regions", "list",
                f"--project={project_id}",
                "--format=value(name)"
            ],
            capture_output=True, text=True, check=True
        )
        regions = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if regions:
            return regions
    except Exception as e:
        log(f"Warning: Failed to query live GCP regions: {e}. Falling back to standard regions.")
    
    # High-reliability SRE fallback list
    return [
        "us-central1", "us-east1", "us-east4", "us-west1", "us-west2",
        "europe-west1", "europe-west2", "europe-west3", "europe-west4",
        "asia-east1", "asia-east2", "asia-northeast1", "asia-northeast2"
    ]

def validate_location(location: str, project_id: str):
    """Verify if the passed GKE location is a valid region or zone."""
    valid_regions = get_valid_regions(project_id)
    
    # Normalize zonal inputs (e.g., us-central1-a -> us-central1)
    region_base = "-".join(location.split("-")[:2])
    
    if location not in valid_regions and region_base not in valid_regions:
        print(f"ERROR: Invalid GKE location '{location}' specified.", file=sys.stdout)
        print(f"Here is a list of possible valid GKE regions in your project:", file=sys.stdout)
        for r in sorted(valid_regions):
            print(f"  - {r}", file=sys.stdout)
        sys.exit(1)

def get_existing_cidrs(project_id: str) -> list[ipaddress.IPv4Network]:
    """Scan GKE clusters in the project and return all active master CIDRs."""
    try:
        log(f"Scanning GKE clusters in project '{project_id}' for master CIDRs...")
        res = subprocess.run(
            [
                "gcloud", "container", "clusters", "list",
                f"--project={project_id}",
                "--format=json(name,privateClusterConfig.masterIpv4CidrBlock)"
            ],
            capture_output=True, text=True, check=True
        )
        data = json.loads(res.stdout)
        cidrs = []
        for item in data:
            cfg = item.get("privateClusterConfig")
            if cfg:
                cidr_str = cfg.get("masterIpv4CidrBlock")
                if cidr_str:
                    try:
                        net = ipaddress.ip_network(cidr_str)
                        cidrs.append(net)
                        log(f"  Found existing GKE Master CIDR: {cidr_str} ({item.get('name')})")
                    except ValueError:
                        pass
        return cidrs
    except Exception as e:
        log(f"Error: Failed to list GKE clusters: {e}")
        sys.exit(1)

def allocate_next_cidr(existing_cidrs: list[ipaddress.IPv4Network]) -> str:
    """Determine the first non-overlapping /28 block inside 172.16.0.0/16."""
    base_net = ipaddress.ip_network("172.16.0.0/16")
    log(f"Calculating next available /28 block inside {base_net}...")

    # Generate all possible /28 subnets
    for candidate in base_net.subnets(new_prefix=28):
        overlap = False
        for existing in existing_cidrs:
            if candidate.overlaps(existing):
                overlap = True
                break
        if not overlap:
            log(f"Allocated non-overlapping CIDR: {candidate}")
            return str(candidate)

    log("Error: IPAM exhausted! No available /28 blocks left in 172.16.0.0/16.")
    sys.exit(1)

def write_cluster_manifest(cluster_name: str, location: str, project_id: str, cidr: str) -> Path:
    """Generate the declarative GKE cluster resource manifest."""
    yaml_path = Path("cluster.yaml")
    manifest = f"""apiVersion: container.cnrm.cloud.google.com/v1beta1
kind: ContainerCluster
metadata:
  name: {cluster_name}
  namespace: agent-system
  annotations:
    cnrm.cloud.google.com/project-id: "{project_id}"
    cnrm.cloud.google.com/remove-default-node-pool: "true"
spec:
  location: "{location}"
  enableAutopilot: true
  privateClusterConfig:
    enablePrivateNodes: true
    enablePrivateEndpoint: false
    masterIpv4CidrBlock: "{cidr}"
"""
    yaml_path.write_text(manifest, encoding="utf-8")
    log(f"Wrote cluster manifest to {yaml_path}")
    return yaml_path

def apply_manifest(path: Path):
    """Execute kubectl apply on the generated manifest."""
    try:
        log(f"Applying cluster manifest inside GKE...")
        subprocess.run(["kubectl", "apply", "-f", str(path)], check=True)
        log("Manifest applied successfully!")
    except Exception as e:
        log(f"Error: Failed to apply cluster manifest: {e}")
        sys.exit(1)

def main():
    if len(sys.argv) < 3:
        print("Usage: operator_agent_provision.py <cluster_name> <location> [project_id]", file=sys.stderr)
        sys.exit(1)

    cluster_name = sys.argv[1]
    location = sys.argv[2]
    
    # 1. Resolve Project ID
    project_id = ""
    if len(sys.argv) >= 4:
        project_id = sys.argv[3]
    if not project_id:
        project_id = get_project_id()
    
    if not project_id:
        print("PROJECT_ID_REQUIRED", file=sys.stdout)
        log("Error: Could not resolve GCP Project ID. Exiting to request user input.")
        sys.exit(0)

    # 2. Validate Location (fast-fail check)
    validate_location(location, project_id)

    # 3. IPAM Allocation
    existing = get_existing_cidrs(project_id)
    allocated_cidr = allocate_next_cidr(existing)

    # 4. Write, Apply & Clean Up
    manifest_path = write_cluster_manifest(cluster_name, location, project_id, allocated_cidr)
    apply_manifest(manifest_path)
    
    # Clean up the temporary GKE Custom Resource manifest
    if manifest_path.exists():
        try:
            manifest_path.unlink()
            log("Temporary GKE Custom Resource manifest cluster.yaml cleaned up.")
        except Exception as e:
            log(f"Warning: Failed to clean up temporary manifest: {e}")
    
    # 5. Register the persistent Operator Agent in state
    agent_id = f"operator-{cluster_name}-{location}"
    add_agent_to_state(agent_id, cluster_name, location, project_id)

    # Output for the AI Agent to parse
    print(f"SUCCESS: {agent_id} | CIDR: {allocated_cidr} | PROJECT: {project_id}", file=sys.stdout)

if __name__ == "__main__":
    main()
