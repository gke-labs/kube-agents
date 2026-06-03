import base64
import json
import os
import sys
import time
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# GKE Secret Volume Mount Location
SECRET_PATH = Path("/etc/github")
APP_ID_FILE = SECRET_PATH / "app-id"
INSTALL_ID_FILE = SECRET_PATH / "installation-id"
KEY_FILE = SECRET_PATH / "private-key"

PORT = int(os.getenv("PORT", "8080"))

def log(msg: str):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [TOKEN-BROKER] {msg}", file=sys.stderr, flush=True)

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
    
    # Sign JWT manually using standard cryptography
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

def get_installation_token(app_id: str, install_id: str, private_key_pem: bytes, repository: str = None) -> tuple[str, str]:
    """Exchange signed JWT for installation access token, optionally scoped to a repository."""
    jwt_token = generate_jwt(app_id, private_key_pem)
    url = f"https://api.github.com/app/installations/{install_id}/access_tokens"
    
    body = {}
    if repository:
        # Extract name from "owner/name" if provided
        repo_name = repository.split("/")[-1]
        body["repositories"] = [repo_name]

    req_data = json.dumps(body).encode("utf-8") if body else None
    
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
    }
    if req_data:
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        url,
        data=req_data,
        headers=headers,
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise RuntimeError(f"GitHub API Error (HTTP {e.code}): {error_body}") from e
    except Exception as e:
        raise RuntimeError(f"Connection error: {e}") from e

    token = data.get("token")
    expires_at = data.get("expires_at")
    if not token:
        raise RuntimeError(f"Token not found in response: {data}")
    return token, expires_at

class TokenBrokerHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Prevent default logging to stdout to keep logs clean
        pass

    def do_GET(self):
        parsed_url = urlparse(self.path)
        
        # 1. Healthcheck Endpoint
        if parsed_url.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"healthy"}')
            return

        # 2. Token Minting Endpoint
        if parsed_url.path == "/token":
            query_params = parse_qs(parsed_url.query)
            repository = query_params.get("repository", [None])[0]

            # Validate secret files exist
            if not (APP_ID_FILE.exists() and INSTALL_ID_FILE.exists() and KEY_FILE.exists()):
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Broker secret configuration files are missing on host."}).encode("utf-8"))
                log("ERROR: GKE Secret mount files missing at /etc/github.")
                return

            try:
                app_id = APP_ID_FILE.read_text().strip()
                install_id = INSTALL_ID_FILE.read_text().strip()
                private_key_pem = KEY_FILE.read_bytes()

                token, expires_at = get_installation_token(app_id, install_id, private_key_pem, repository)
                
                response_data = {
                    "token": token,
                    "expires_at": expires_at,
                    "repository": repository
                }
                
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(response_data).encode("utf-8"))
                log(f"Successfully issued token for repository: {repository or 'all'}")
                
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                log(f"ERROR: Failed to mint token: {e}")
            return

        # 3. Not Found
        self.send_response(404)
        self.end_headers()

def main():
    log(f"Starting GitHub Token Broker on port {PORT}...")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), TokenBrokerHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    log("Stopping Token Broker...")

if __name__ == "__main__":
    main()
