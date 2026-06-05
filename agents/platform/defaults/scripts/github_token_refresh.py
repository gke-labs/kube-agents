#!/opt/hermes/.venv/bin/python3
"""
GKE Platform Agent — Secure GitHub Token Refresher (Broker Client)

This script queries the internal cluster-local Token Broker to retrieve
a short-lived (1-hour), repository-scoped installation token, and securely
caches it inside the git credentials store and GitHub CLI.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

TOKEN_BROKER_URL = os.getenv("TOKEN_BROKER_URL", "http://github-token-minter.agent-system.svc.cluster.local:8080/token")

def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [SRE-AUTH] {msg}", file=sys.stderr, flush=True)

def get_current_git_repo() -> str:
    """Extract repository name (owner/repo) from local git config."""
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=True
        )
        url = res.stdout.strip().strip("/")
        # Parse owner/repo from URL (supports HTTPS and SSH formats)
        # e.g., git@github.com:owner/repo.git or https://github.com/owner/repo.git
        if url.endswith(".git"):
            url = url[:-4]
        # Remove protocol prefix if present (e.g. https://)
        if "://" in url:
            url = url.split("://", 1)[1]
        # If SSH format, split by ':' (e.g. git@github.com:owner/repo)
        if "@" in url and ":" in url:
            url = url.split(":", 1)[1]
        
        parts = url.split("/")
        if len(parts) >= 2:
            return f"{parts[-2]}/{parts[-1]}"
    except Exception as e:
        log(f"WARNING: Could not parse repository from git config: {e}")
    return None

def refresh_git_credentials() -> str:
    """Query local Minty, retrieve token, and cache inside git credentials."""
    # 1. Read the GKE Service Account token (OIDC token)
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    try:
        with open(token_path, "r", encoding="utf-8") as f:
            oidc_token = f.read().strip()
    except Exception as e:
        raise RuntimeError(f"Failed to read service account token from {token_path}: {e}")

    # 2. Dynamically identify target repository from workspace git remote
    repository = get_current_git_repo()
    if not repository:
        raise RuntimeError("Could not identify target repository from git config")
    if "/" not in repository:
        raise RuntimeError(f"Invalid repository format parsed from git config: {repository}")

    org_name, repo_name = repository.split("/", 1)

    headers = {
        "Content-Type": "application/json",
        "X-OIDC-Token": oidc_token
    }
    body = {
        "org_name": org_name,
        "repositories": [repo_name],
        "scope": "platform-agent-scope"
    }
    req_data = json.dumps(body).encode("utf-8")

    log(f"Requesting scoped installation token from Minty for repository: {org_name}/{repo_name}...")
    
    try:
        req = urllib.request.Request(
            TOKEN_BROKER_URL,
            data=req_data,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            token = response.read().decode("utf-8").strip()
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise RuntimeError(f"Minty returned error (HTTP {e.code}): {error_body}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to connect to Minty at {TOKEN_BROKER_URL}: {e}") from e

    if not token:
        raise RuntimeError("Token received from Minty is empty")

    # 2. Configure Git with strict owner-only (0600) permissions to protect the plaintext token
    subprocess.run(["git", "config", "--global", "credential.helper", "store"], check=True)
    creds_file = Path.home() / ".git-credentials"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    mode = 0o600
    with os.fdopen(os.open(creds_file, flags, mode), "w", encoding="utf-8") as f:
        f.write(f"https://x-access-token:{token}@github.com\n")
    
    # 3. Configure GitHub CLI
    subprocess.run(["gh", "auth", "login", "--with-token"], input=token, text=True, check=True)
    
    log("Git credentials store successfully refreshed from Token Broker! Token cached.")
    return token

def main():
    try:
        refresh_git_credentials()
    except Exception as e:
        log(f"FATAL: Failed to refresh git credentials: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
