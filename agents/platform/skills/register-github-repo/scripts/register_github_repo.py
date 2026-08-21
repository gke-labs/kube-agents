#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import sys
import time

# Append scripts paths to allow importing platform utilities
sys.path.append(os.path.join(os.path.dirname(__file__), "../../../scripts"))
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")

from github_token_refresh import refresh_git_credentials
from gitops_workspace import get_managed_repos

BARE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

def run(cmd, **kwargs):
    return subprocess.run(cmd, text=True, **kwargs)

def register_repo(repo: str) -> int:
    repo = (repo or "").strip()
    if not repo or not BARE_REPO_RE.match(repo):
        print(
            "Error: Invalid repository format. Please provide a valid 'owner/repo' string.",
            file=sys.stderr,
        )
        return 1

    cfg_name = os.environ.get("GITHUB_STATE_CONFIGMAP", "platform-agent-github-state")
    ns = os.environ.get("KUBE_DEFAULT_NAMESPACE", "kubeagents-system")

    try:
        repos = get_managed_repos()
    except RuntimeError as e:
        print(f"Error reading ConfigMap: {e}", file=sys.stderr)
        return 1

    if repo in repos:
        print(f"Repository {repo} is already in the managed list.")
        return 0

    repos.append(repo)
    new_repos_str = ", ".join(repos)
    patch = {"data": {"managed_repos": new_repos_str}}

    print(f"Patching ConfigMap {cfg_name} to generate Minty policy for {repo}...")
    try:
        run(
            ["kubectl", "patch", "configmap", cfg_name, "-n", ns, "--type=merge", "-p", json.dumps(patch)],
            capture_output=True,
            check=True,
        )
    except FileNotFoundError:
        print("Error: 'kubectl' binary not found in PATH.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to update ConfigMap: {e.stderr or e}", file=sys.stderr)
        return 1

    print(f"Minting new Github Token for {repo}...")
    mint_success = False
    last_err = None
    # Wait for the operator to sync the token minter policy...
    for attempt in range(10):
        try:
            refresh_git_credentials(repo)
            mint_success = True
            break
        except Exception as e:
            last_err = e
            time.sleep(2)
            
    if not mint_success:
        print(f"Error: Failed to mint token for {repo}. Ensure Token Broker has access. ({last_err})", file=sys.stderr)
        print("Rolling back ConfigMap patch...")
        repos.remove(repo)
        rollback_patch = {"data": {"managed_repos": ", ".join(repos)}}
        run(["kubectl", "patch", "configmap", cfg_name, "-n", ns, "--type=merge", "-p", json.dumps(rollback_patch)], capture_output=True)
        return 1

    print(f"Verifying access to {repo}...")
    try:
        run(["gh", "repo", "view", repo, "--json", "id"], capture_output=True, check=True)
    except FileNotFoundError:
        print("Error: 'gh' CLI tool not found in PATH.", file=sys.stderr)
        print("Rolling back ConfigMap patch...")
        repos.remove(repo)
        rollback_patch = {"data": {"managed_repos": ", ".join(repos)}}
        run(["kubectl", "patch", "configmap", cfg_name, "-n", ns, "--type=merge", "-p", json.dumps(rollback_patch)], capture_output=True)
        return 1
    except subprocess.CalledProcessError:
        print(
            f"Error: Agent's GitHub App does not have access to {repo}. Please install the GitHub app on the repository.",
            file=sys.stderr,
        )
        print("Rolling back ConfigMap patch...")
        repos.remove(repo)
        rollback_patch = {"data": {"managed_repos": ", ".join(repos)}}
        run(["kubectl", "patch", "configmap", cfg_name, "-n", ns, "--type=merge", "-p", json.dumps(rollback_patch)], capture_output=True)
        return 1

    print(f"Successfully verified access and added {repo} to {cfg_name}.")
    return 0

def main():
    parser = argparse.ArgumentParser(description="Verify and register a new GitHub repo to the agent ConfigMap.")
    parser.add_argument("--repo", required=True, help="Repository in owner/repo format")
    args = parser.parse_args()

    sys.exit(register_repo(args.repo))

if __name__ == "__main__":
    main()
