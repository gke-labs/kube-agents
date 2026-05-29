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
    log(f"Pushing active branch '{branch_name}' securely to origin...")
    subprocess.run(["git", "push", "-f", "origin", branch_name], check=True)

def create_pull_request(token: str, branch: str, title: str, body: str) -> str:
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
    parser.add_argument("--project", required=True, help="Target GCP project ID")
    
    args = parser.parse_args()
    
    try:
        # Secure dynamic token exchange & Git/gh credentials configuration
        token = refresh_git_credentials()
        
        # Git branch pushing
        push_branch(args.branch)
        
        # Submit Pull Request
        pr_url = create_pull_request(token, args.branch, args.title, args.body)
        log(f"PR SUBMITTED SUCCESSFULY! 🏆 URL: {pr_url}")
        
        # Print raw URL to stdout for the MCP tool to parse
        print(pr_url)
        
    except Exception as e:
        log(f"FATAL ERROR: GitOps suggestion submission failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
