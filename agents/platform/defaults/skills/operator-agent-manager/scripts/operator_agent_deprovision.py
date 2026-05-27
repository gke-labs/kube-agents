#!/usr/bin/env python3
# operator_agent_deprovision.py - Dynamic GKE Autopilot de-provisioner and state manager.
# Deletes the GKE cluster resource manifest and removes the agent from the tracking JSONL file.

import json
import sys
import os
import subprocess
from pathlib import Path
from datetime import datetime

def log(msg: str):
    print(f"[IPAM-DEPROVISIONER] {msg}", file=sys.stderr)

def get_hermes_home() -> Path:
    """Return the active HERMES_HOME directory."""
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))

def get_state_file() -> Path:
    """Return the path to the operator agents JSONL file."""
    return get_hermes_home() / "operator_agents.jsonl"

def remove_agent_from_state(agent_id: str):
    """Remove the agent entry from the JSONL state file."""
    state_file = get_state_file()
    if not state_file.exists():
        log("Warning: State file operator_agents.jsonl does not exist. Skipping state update.")
        return

    log(f"Removing agent '{agent_id}' from state file: {state_file}")
    lines = []
    removed = False
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                entry = json.loads(line)
                if entry.get("agent_id") == agent_id:
                    removed = True
                    log(f"  Found and removed state entry: {line.strip()}")
                    continue
                lines.append(line)
        
        if removed:
            # Write back the filtered lines
            with open(state_file, "w", encoding="utf-8") as f:
                f.writelines(lines)
            log("State file updated successfully.")
        else:
            log("Warning: No matching agent entry found in state file.")
    except Exception as e:
        log(f"Error: Failed to update state file: {e}")

def delete_cluster_manifest(cluster_name: str):
    """Delete the GKE cluster custom resource from GKE."""
    try:
        log(f"Deleting GKE cluster Custom Resource 'mercury-05' inside namespace 'agent-system'...")
        # Run delete command
        subprocess.run(
            ["kubectl", "delete", "containercluster", cluster_name, "-n", "agent-system"],
            check=True
        )
        log("Cluster custom resource deleted successfully! GCP will de-provision the cluster in the background.")
    except Exception as e:
        log(f"Error: Failed to delete cluster manifest: {e}")
        sys.exit(1)

def main():
    if len(sys.argv) < 3:
        print("Usage: operator_agent_deprovision.py <cluster_name> <location>", file=sys.stderr)
        sys.exit(1)

    cluster_name = sys.argv[1]
    location = sys.argv[2]
    agent_id = f"operator-{cluster_name}-{location}"

    # 1. Delete GKE cluster resource manifest
    delete_cluster_manifest(cluster_name)

    # 2. Remove from persistent JSONL state
    remove_agent_from_state(agent_id)

    # Output for the AI Agent to parse
    print(f"SUCCESS: {agent_id} DELETED", file=sys.stdout)

if __name__ == "__main__":
    main()
