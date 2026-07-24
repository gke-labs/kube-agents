#!/opt/hermes/.venv/bin/python3
"""
GKE Platform Agent — GitOps PR Suggestion Submitter

This script automates GKE-to-GitHub App branch pushing and Pull Request creation.
It cleanly reuses the secure token refresh logic from github_token_refresh.py natively.
"""

import argparse
import subprocess
import sys
# Append global scripts path to allow importing the token refresher
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")

from github_token_refresh import refresh_git_credentials, log

def push_branch(branch_name: str):
    """Push the active git branch to the remote origin securely."""
    protected_branches = {"main", "master", "production"}
    clean_branch = branch_name.strip().lower()
    if clean_branch in protected_branches:
        raise ValueError(f"CRITICAL SECURITY REFUSAL: Force-pushing to protected branch '{branch_name}' is strictly blocked by GKE SRE guardrails!")

    log(f"Pushing active branch '{branch_name}' securely to origin...")
    subprocess.run(["git", "push", "-f", "origin", branch_name], check=True)

def create_pull_request(branch: str, title: str, body: str) -> str:
    """Submit the Pull Request securely using the GitHub CLI (gh)."""
    log(f"Submitting GitOps Pull Request for branch '{branch}'...")
    
    cmd = [
        "gh", "pr", "create",
        "--title", title,
        "--body", body,
        "--base", "main",
        "--head", branch
    ]
    
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    pr_url = res.stdout.strip()
    return pr_url

def main():
    parser = argparse.ArgumentParser(description="Secure GitOps PR Suggestion Submitter")
    parser.add_argument("--branch", required=True, help="Active Git branch name")
    parser.add_argument("--title", required=True, help="Pull Request title")
    parser.add_argument("--body", required=True, help="Pull Request description body")
    
    args = parser.parse_args()
    
    try:
        # Secure dynamic token exchange & Git/gh credentials configuration
        refresh_git_credentials()
        
        # Git branch pushing
        push_branch(args.branch)
        
        # Submit Pull Request
        pr_url = create_pull_request(args.branch, args.title, args.body)
        log(f"PR SUBMITTED SUCCESSFULLY! 🏆 URL: {pr_url}")
        
        # Print raw URL to stdout for the MCP tool to parse
        print(pr_url)
        
    except subprocess.CalledProcessError as e:
        log("FATAL ERROR: GitOps subprocess execution failed!")
        log(f"Exit Code: {e.returncode}")
        if e.stderr:
            log(f"Stderr Output:\n{e.stderr.strip()}")
        if e.stdout:
            log(f"Stdout Output:\n{e.stdout.strip()}")
        sys.exit(1)
    except Exception as e:
        log(f"FATAL ERROR: GitOps suggestion submission failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
