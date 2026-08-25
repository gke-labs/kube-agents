#!/opt/hermes/.venv/bin/python3
"""
GKE Platform Agent — Secure GitHub Token Refresher (Broker Client)

In the agent sandbox this script asks the credential sidecar to refresh. Only
the sidecar queries the token broker (Minty) directly. Standalone/legacy
deployments continue to use the direct path.
"""

import email.message
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

from credential_proxy_client import authorization_headers


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [SRE-AUTH] {msg}", file=sys.stderr, flush=True)


TOKEN_BROKER_URL = os.getenv(
    "TOKEN_BROKER_URL",
    "http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token",
)


def get_current_git_repo() -> str | None:
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=True,
        )
        url = res.stdout.strip()
        if "github.com" in url:
            path = url.split("github.com")[-1].lstrip(":").lstrip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return path
    except Exception:
        pass
    return None


def refresh_git_credentials(
    target_repo: str | None = None,
    *,
    max_attempts: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
) -> str:
    repository = target_repo.strip().strip("/") if target_repo else get_current_git_repo()

    if not repository or "/" not in repository:
        raise RuntimeError(
            f"Could not identify target repository '{repository}'. Must be in 'owner/repo' format."
        )

    proxy_url = os.getenv("CREDENTIAL_PROXY_URL", "").strip()
    if proxy_url:
        # In the agent sandbox: delegate to the credential sidecar.
        # The sidecar manages bounded retries against Minty internally.
        # The client uses a 60s timeout to allow the sidecar's retry budget
        # to finish, and fails fast on any error without re-triggering retries.
        url = proxy_url.rstrip("/") + "/v1/github/refresh"
        request = urllib.request.Request(
            url,
            data=json.dumps({"repository": repository}).encode("utf-8"),
            # Empty in the sidecar deployment; carries the caller's projected
            # ServiceAccount token when the broker runs in its own Pod.
            headers={"Content-Type": "application/json", **authorization_headers()},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status == 200:
                    log(
                        f"GitHub credentials refreshed in credential sidecar for {repository}."
                    )
                    return ""
                raise RuntimeError(
                    f"Credential sidecar rejected refresh: HTTP {response.status}"
                )
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"Credential sidecar failed to refresh GitHub auth: HTTP {exc.code}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Credential sidecar failed to refresh GitHub auth: {exc}"
            ) from exc

    # 1. Retrieve Google OIDC identity token via gcloud external command
    oidc_token = None
    try:
        res = subprocess.run(
            [
                "gcloud",
                "auth",
                "print-identity-token",
                f"--audiences={TOKEN_BROKER_URL}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        oidc_token = res.stdout.strip()
    except Exception:
        try:
            res = subprocess.run(
                ["gcloud", "auth", "print-identity-token"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            oidc_token = res.stdout.strip()
        except Exception as e:
            raise RuntimeError(
                f"Failed to retrieve Google OIDC token via gcloud: {e}"
            ) from e

    if not oidc_token:
        raise RuntimeError("Retrieved Google OIDC token via gcloud is empty.")

    # 2. Query Minty Token Broker with bounded retries
    org_name, repo_name = repository.split("/", 1)
    headers = {"Content-Type": "application/json", "X-OIDC-Token": oidc_token}
    body = {
        "org_name": org_name,
        "repositories": [repo_name],
        "scope": "platform-agent-scope",
    }
    req_data = json.dumps(body).encode("utf-8")

    log(
        f"Requesting scoped installation token from Minty for repository: {org_name}/{repo_name}..."
    )

    token = None
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                TOKEN_BROKER_URL, data=req_data, headers=headers, method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    token = response.read().decode("utf-8").strip()
                    break
                if response.status >= 500:
                    raise urllib.error.HTTPError(
                        TOKEN_BROKER_URL,
                        response.status,
                        f"HTTP {response.status}",
                        email.message.Message(),
                        None,
                    )
                error_body = response.read().decode("utf-8").strip()
                raise RuntimeError(
                    f"Minty returned error (HTTP {response.status}): {error_body}"
                )
        except urllib.error.HTTPError as e:
            last_exc = e
            error_body = ""
            try:
                error_body = e.read().decode("utf-8")
            except Exception:
                pass
            if e.code >= 500:
                if attempt < max_attempts:
                    delay = initial_delay * (backoff_factor ** (attempt - 1))
                    log(
                        f"Minty returned HTTP {e.code} on attempt {attempt}/{max_attempts}; retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                    continue
            raise RuntimeError(
                f"Minty returned error (HTTP {e.code}): {error_body}"
            ) from e
        except (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as e:
            last_exc = e
            if attempt < max_attempts:
                delay = initial_delay * (backoff_factor ** (attempt - 1))
                log(
                    f"Minty connection error ({e}) on attempt {attempt}/{max_attempts}; retrying in {delay:.1f}s..."
                )
                time.sleep(delay)
                continue
            raise RuntimeError(
                f"Failed to connect to Minty at {TOKEN_BROKER_URL}: {e}"
            ) from e
        except Exception as e:
            raise RuntimeError(
                f"Failed to connect to Minty at {TOKEN_BROKER_URL}: {e}"
            ) from e

    if not token:
        if last_exc:
            raise RuntimeError(
                f"Failed to obtain token from Minty: {last_exc}"
            ) from last_exc
        raise RuntimeError("Token received from Minty is empty")

    # 3. Configure gh CLI authentication and Git credentials
    try:
        env = os.environ.copy()
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)
        subprocess.run(
            ["gh", "auth", "login", "--with-token"],
            input=token,
            text=True,
            check=True,
            capture_output=True,
            timeout=15,
            env=env,
        )
        subprocess.run(
            ["gh", "auth", "setup-git"],
            check=True,
            capture_output=True,
            timeout=15,
            env=env,
        )
        log(
            f"GitHub authentication successfully configured for repository: {repository}"
        )
    except Exception as e:
        raise RuntimeError(f"Failed to configure GitHub auth in gh CLI: {e}") from e

    return token


def main():
    target_repo = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        refresh_git_credentials(target_repo)
    except Exception as e:
        log(f"FATAL: Failed to refresh git credentials: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
