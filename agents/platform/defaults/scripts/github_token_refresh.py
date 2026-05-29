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
from pathlib import Path

SECRET_PATH = Path("/etc/github")
APP_ID_FILE = SECRET_PATH / "app-id"
INSTALL_ID_FILE = SECRET_PATH / "installation-id"
KEY_FILE = SECRET_PATH / "private-key"

def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [SRE-AUTH] {msg}", file=sys.stderr, flush=True)

def generate_jwt(app_id: str, private_key_pem: bytes) -> str:
    """Generate a signed RS256 JWT valid for 10 minutes."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.backends import default_backend
        import jwt
    except ImportError:
        jwt = None

    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + (10 * 60),
        "iss": app_id
    }

    if jwt:
        private_key = serialization.load_pem_private_key(
            private_key_pem, password=None, backend=default_backend()
        )
        return jwt.encode(payload, private_key, algorithm="RS256")
    
    # Fallback using standard cryptography
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes

    header = {"alg": "RS256", "typ": "JWT"}
    
    def b64_url(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")

    segments = [
        b64_url(json.dumps(header, separators=(",", ":")).encode("utf-8")),
        b64_url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    ]
    
    signing_input = ".".join(segments).encode("utf-8")
    private_key = serialization.load_pem_private_key(
        private_key_pem, password=None, backend=default_backend()
    )
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
    cmd = [
        "curl", "-s", "-X", "POST",
        "-H", f"Authorization: Bearer {jwt_token}",
        "-H", "Accept: application/vnd.github+json",
        f"https://api.github.com/app/installations/{install_id}/access_tokens"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(res.stdout)
    token = data.get("token")
    if not token:
        raise RuntimeError(f"Failed to retrieve token: {res.stdout}")
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

    # Configure Git
    subprocess.run(["git", "config", "--global", "credential.helper", "store"], check=True)
    creds_file = Path.home() / ".git-credentials"
    with open(creds_file, "w", encoding="utf-8") as f:
        f.write(f"https://x-access-token:{token}@github.com\n")
    
    # Configure GitHub CLI
    subprocess.run(["gh", "auth", "login", "--with-token"], input=token, text=True, check=True)
    
    log("Git credentials store successfully refreshed! Token cached.")
    return token

def main():
    try:
        token = refresh_git_credentials()
        # Output the token to stdout for dynamic script usage if needed
        print(token, file=sys.stdout)
    except Exception as e:
        log(f"FATAL: Failed to refresh git credentials: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
