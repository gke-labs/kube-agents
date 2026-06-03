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

TOKEN_BROKER_URL = os.getenv("TOKEN_BROKER_URL", "http://github-token-broker.agent-system.svc.cluster.local:8080/token")

def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [SRE-AUTH] {msg}", file=sys.stderr, flush=True)

def get_current_git_repo() -> str:
    """Extract repository name (owner/repo) from local git config."""
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=True
        )
        url = res.stdout.strip()
        # Parse owner/repo from URL (supports HTTPS and SSH formats)
        # e.g., git@github.com:owner/repo.git or https://github.com/owner/repo.git
        if url.endswith(".git"):
            url = url[:-4]
        if ":" in url:
            return url.split(":")[-1]
        elif "/" in url:
            parts = url.split("/")
            return f"{parts[-2]}/{parts[-1]}"
    except Exception as e:
        log(f"WARNING: Could not parse repository from git config: {e}")
    return None

def refresh_git_credentials() -> str:
    """Query local Token Broker, retrieve token, and cache inside git credentials."""
    # 1. Dynamically identify target repository from workspace git remote
    repository = get_current_git_repo()
    
    url = TOKEN_BROKER_URL
    if repository:
        url += f"?repository={repository}"

    log(f"Requesting scoped installation token from broker for repository: {repository or 'any'}...")
    
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise RuntimeError(f"Token Broker returned error (HTTP {e.code}): {error_body}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to connect to Token Broker at {TOKEN_BROKER_URL}: {e}") from e

    token = data.get("token")
    if not token:
        raise RuntimeError(f"Token not found in broker response: {data}")

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
