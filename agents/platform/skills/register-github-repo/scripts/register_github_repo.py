#!/usr/bin/env python3
import argparse
import subprocess
import json
import os
import sys

# Append global scripts path to allow importing the token refresher
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")

from github_token_refresh import refresh_git_credentials

def run(cmd, **kwargs):
    return subprocess.run(cmd, text=True, **kwargs)

def main():
    parser = argparse.ArgumentParser(description="Verify and register a new GitHub repo to the agent ConfigMap.")
    parser.add_argument("--repo", required=True, help="Repository in owner/repo format")
    args = parser.parse_args()

    cfg_name = os.environ.get("GITHUB_STATE_CONFIGMAP", "platform-agent-github-state")

    repo = args.repo.strip()
    if repo.count("/") != 1:
        print("Error: Invalid repository format. Please provide a valid 'owner/repo' string.", file=sys.stderr)
        sys.exit(1)

    print(f"Minting new Github Token for {repo}...")
    try:
        # Call the existing token refresher script
        refresh_git_credentials(repo)
    except Exception as e:
        print(f"Error: Failed to mint token for {repo}. Ensure Token Broker has access. ({e})", file=sys.stderr)
        sys.exit(1)
        
    print(f"Verifying access to {repo}...")
    try:
        run(["gh", "repo", "view", repo, "--json", "id"], capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Error: Agent's GitHub App does not have access to {repo}. Please install the GitHub app on the repository.", file=sys.stderr)
        sys.exit(1)
        
    print(f"Access verified. Patching ConfigMap {cfg_name}...")
    
    ns = os.environ.get("KUBE_DEFAULT_NAMESPACE", "kubeagents-system")
    # Check if configmap exists
    cm_res = run(["kubectl", "get", "configmap", cfg_name, "-n", ns, "-o", "json"], capture_output=True)
    if cm_res.returncode != 0:
        print(f"Error: ConfigMap {cfg_name} not found in namespace {ns}. It must be provisioned by the operator.", file=sys.stderr)
        sys.exit(1)
        
    cm = json.loads(cm_res.stdout)
    data = cm.get("data", {})
    current_repos_str = data.get("managed_repos", "")
    
    repos = [r.strip() for r in current_repos_str.split(",") if r.strip()]
    
    if repo in repos:
        print(f"Repository {repo} is already in the managed list.")
        sys.exit(0)
        
    repos.append(repo)
    new_repos_str = ", ".join(repos)
    
    patch = {"data": {"managed_repos": new_repos_str}}
    try:
        run(["kubectl", "patch", "configmap", cfg_name, "-n", ns, "--type=merge", "-p", json.dumps(patch)], check=True)
        print(f"Successfully added {repo} to {cfg_name}.")
    except subprocess.CalledProcessError:
        print(f"Error: Failed to update ConfigMap.", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
