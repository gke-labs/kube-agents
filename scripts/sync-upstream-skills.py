#!/usr/bin/env python3
"""Syncs matching agent skills from the upstream gke-mcp repository."""

import os
import shutil
import subprocess
import sys
import tempfile

UPSTREAM_REPO = "https://github.com/GoogleCloudPlatform/gke-mcp.git"

SKILL_MAPPINGS = {
    "gke-app-onboarding": ["platform"],
    "gke-backup-dr": ["platform"],
    "gke-cluster-creator": ["platform"],
    "gke-cluster-lifecycle": ["platform"],
    "gke-compute-classes": ["platform"],
    "gke-cost-analysis": ["platform"],
    "gke-inference-quickstart": ["platform"],
    "gke-multi-tenancy": ["platform"],
    "gke-networking-edge": ["platform"],
    "gke-observability": ["platform"],
    "gke-productionize": ["platform"],
    "gke-reliability": ["platform"],
    "gke-storage": ["platform"],
    "gke-workload-scaling": ["platform"],
    "gke-workload-security": ["platform"]
}

def run_cmd(cmd, cwd=None):
    """Runs a shell command and returns the result, raising an exception on failure."""
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error running command: {' '.join(cmd)}", file=sys.stderr)
        print(f"Stdout:\n{res.stdout}", file=sys.stderr)
        print(f"Stderr:\n{res.stderr}", file=sys.stderr)
        res.check_returncode()
    return res

def main():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    
    try:
        print("Creating temporary directory for sparse checkout...")
        with tempfile.TemporaryDirectory() as tmpdir:
            print(f"Cloning upstream repository (sparse, depth 1): {UPSTREAM_REPO}...")
            run_cmd([
                "git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
                UPSTREAM_REPO, tmpdir
            ])
            
            print("Configuring sparse-checkout to retrieve only skills directory...")
            run_cmd(["git", "sparse-checkout", "set", "skills"], cwd=tmpdir)
            
            upstream_skills_dir = os.path.join(tmpdir, "skills")
            if not os.path.isdir(upstream_skills_dir):
                print(f"Error: upstream skills directory not found in clone: {upstream_skills_dir}", file=sys.stderr)
                sys.exit(1)
                
            print("\nSyncing skills...")
            for skill_name, agents in SKILL_MAPPINGS.items():
                src_skill_path = os.path.join(upstream_skills_dir, skill_name)
                if not os.path.isdir(src_skill_path):
                    print(f"Warning: Upstream skill '{skill_name}' not found at {src_skill_path}. Skipping.")
                    continue
                    
                for agent in agents:
                    dest_path = os.path.join(repo_root, "agents", agent, "skills", skill_name)
                    print(f"Syncing '{skill_name}' to agents/{agent}/skills/{skill_name}...")
                    
                    # Delete existing destination directory to remove stale files
                    if os.path.exists(dest_path):
                        shutil.rmtree(dest_path)
                        
                    # Re-create destination parent directories if needed
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    
                    # Copy from sparse-checkout src to dest
                    shutil.copytree(src_skill_path, dest_path)
                    
            print("\nSynchronization complete!")
    except subprocess.CalledProcessError:
        print("\nError: Synchronization failed due to command error. Details above.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nError: An unexpected error occurred: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
