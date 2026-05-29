#!/opt/hermes/.venv/bin/python3
"""
GKE Platform Agent — Secure GitHub App Token Refresher

This script handles GKE-to-GitHub App JWT exchange and securely caches
the short-lived 1-hour installation token inside git credentials store.
It can be run stand-alone by the agent to self-heal from git authentication errors,
or imported/reused by other scripts.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

try:
    import jwt
except ImportError:
    jwt = None

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

SECRET_PATH = Path("/etc/github")
APP_ID_FILE = SECRET_PATH / "app-id"
INSTALL_ID_FILE = SECRET_PATH / "installation-id"
KEY_FILE = SECRET_PATH / "private-key"

def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [SRE-AUTH] {msg}", file=sys.stderr, flush=True)

def generate_jwt(app_id: str, private_key_pem: bytes) -> str:
    """Generate a signed RS256 JWT valid for 10 minutes."""
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": app_id
    }

    private_key = serialization.load_pem_private_key(
        private_key_pem, password=None, backend=default_backend()
    )

    if jwt:
        return jwt.encode(payload, private_key, algorithm="RS256")
    
    # Fallback using standard cryptography
    header = {"alg": "RS256", "typ": "JWT"}
    
    def b64_url(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")

    segments = [
        b64_url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        b64_url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    ]
    
    signing_input = ".".join(segments).encode("utf-8")
    signature = private_key.sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA256()
    )
    segments.append(b64_url(signature))
    return ".".join(segments)


def get_installation_token(app_id: str, install_id: str, private_key_pem: bytes) -> str:
    """Exchange signed JWT for installation access token."""
    jwt_token = generate_jwt(app_id, private_key_pem)
    url = f"https://api.github.com/app/installations/{install_id}/access_tokens"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github+json",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise RuntimeError(f"Failed to retrieve token (HTTP {e.code}): {error_body}") from e
    except Exception as e:
        raise RuntimeError(f"Failed to retrieve token: {e}") from e

    token = data.get("token")
    if not token:
        raise RuntimeError(f"Token not found in response: {data}")
    return token

def refresh_git_credentials() -> str:
    """Securely read GKE secrets, exchange token, and cache inside git credentials."""
    if not (APP_ID_FILE.exists() and INSTALL_ID_FILE.exists() and KEY_FILE.exists()):
        raise FileNotFoundError(f"GKE Secret mount missing at {SECRET_PATH}.")

    app_id = APP_ID_FILE.read_text().strip()
    install_id = INSTALL_ID_FILE.read_text().strip()
    private_key_pem = KEY_FILE.read_bytes()

    log("Exchanging JWT for GHE Installation Token...")
    token = get_installation_token(app_id, install_id, private_key_pem)

    # Configure Git with strict owner-only (0600) permissions to protect the plaintext token
    subprocess.run(["git", "config", "--global", "credential.helper", "store"], check=True)
    creds_file = Path.home() / ".git-credentials"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    mode = 0o600
    with os.fdopen(os.open(creds_file, flags, mode), "w", encoding="utf-8") as f:
        f.write(f"https://x-access-token:{token}@github.com\n")
    
    # Configure GitHub CLI
    subprocess.run(["gh", "auth", "login", "--with-token"], input=token, text=True, check=True)
    
    log("Git credentials store successfully refreshed! Token cached.")
    return token

def main():
    try:
        refresh_git_credentials()
    except Exception as e:
        log(f"FATAL: Failed to refresh git credentials: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
