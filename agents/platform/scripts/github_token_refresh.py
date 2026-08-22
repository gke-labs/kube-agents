#!/opt/hermes/.venv/bin/python3
"""
GKE Platform Agent — Secure GitHub Token Refresher (Broker Client)

In the agent sandbox this script asks the credential sidecar to refresh. Only
the sidecar queries Minty and caches the short-lived repository-scoped token.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

TOKEN_BROKER_URL = os.getenv(
    "TOKEN_BROKER_URL",
    "http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token",
)


def log(msg: str):
    print(
        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [SRE-AUTH] {msg}",
        file=sys.stderr,
        flush=True,
    )


def get_current_git_repo() -> str:
    """Extract repository name (owner/repo) from local git config."""
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=True,
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


def refresh_git_credentials(
    target_repo: str | None = None,
    *,
    max_attempts: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
) -> str:
    """Query local Minty, retrieve token, and cache inside git credentials."""
    repository = target_repo.strip().strip("/") if target_repo else get_current_git_repo()
    if not repository or "/" not in repository:
        raise RuntimeError("Could not identify target repository as owner/name")

    proxy_url = os.getenv("CREDENTIAL_PROXY_URL", "").strip()
    if proxy_url:
        # Client leg (agent sandbox -> sidecar proxy over loopback).
        # The sidecar runs the internal helper which already does bounded retry
        # against Minty (budgeted at ~20s max). The client uses a 45s socket timeout
        # to allow the sidecar to finish its attempts. If the sidecar answers with
        # an HTTP status (200, 4xx, or 502), the client accepts it immediately
        # without nested retries. It only retries if the loopback connection itself drops
        # (e.g. sidecar restart).
        url = proxy_url.rstrip("/") + "/v1/github/refresh"
        request = urllib.request.Request(
            url,
            data=json.dumps({"repository": repository}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        last_exc = None
        proxy_attempts = 2
        for attempt in range(1, proxy_attempts + 1):
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    if response.status == 200:
                        log(
                            f"GitHub credentials refreshed in credential sidecar for {repository}."
                        )
                        return ""
                    raise RuntimeError(
                        f"Credential sidecar rejected refresh: HTTP {response.status}"
                    )
            except urllib.error.HTTPError as exc:
                # The sidecar returned an HTTP error response. The sidecar has already
                # executed its retries internally. Fail fast with the sidecar's status.
                raise RuntimeError(
                    f"Credential sidecar failed to refresh GitHub auth: HTTP {exc.code}"
                ) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt < proxy_attempts:
                    log(
                        f"Credential sidecar transport error ({exc}) on attempt {attempt}/{proxy_attempts}; retrying in {initial_delay:.1f}s..."
                    )
                    time.sleep(initial_delay)
                    continue
                raise RuntimeError(
                    f"Credential sidecar failed to refresh GitHub auth: {exc}"
                ) from exc
            except Exception as exc:
                raise RuntimeError(
                    "Credential sidecar failed to refresh GitHub auth"
                ) from exc

        raise RuntimeError(
            "Credential sidecar failed to refresh GitHub auth"
        ) from last_exc

    # 1. Retrieve Google OIDC identity token via gcloud external command
    oidc_token = None
    try:
        oidc_token = subprocess.run(
            ["gcloud", "auth", "print-identity-token", f"--audiences={TOKEN_BROKER_URL}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except Exception as e1:
        # If --audiences fails (e.g., when running with human user credentials), retry without flags
        try:
            oidc_token = subprocess.run(
                ["gcloud", "auth", "print-identity-token"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            ).stdout.strip()
        except Exception as e2:
            raise RuntimeError(
                f"Failed to retrieve Google OIDC token via gcloud: {e2}"
            ) from e2

    if not oidc_token:
        raise RuntimeError("Retrieved Google OIDC token via gcloud is empty")

    # 2. Dynamically identify target repository from workspace git remote or parameter
    org_name, repo_name = repository.split("/", 1)

    headers = {
        "Content-Type": "application/json",
        "X-OIDC-Token": oidc_token,
    }
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
                TOKEN_BROKER_URL,
                data=req_data,
                headers=headers,
                method="POST",
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
                        response.headers,
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
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_exc = e
            if attempt < max_attempts:
                delay = initial_delay * (backoff_factor ** (attempt - 1))
                log(
                    f"Minty transport error ({e}) on attempt {attempt}/{max_attempts}; retrying in {delay:.1f}s..."
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

    # 3. Configure GitHub CLI to securely cache the token in its internal state.
    env = os.environ.copy()
    if "GITHUB_TOKEN" in env:
        del env["GITHUB_TOKEN"]
    if "GH_TOKEN" in env:
        del env["GH_TOKEN"]
    subprocess.run(
        ["gh", "auth", "login", "--with-token"],
        input=token,
        text=True,
        env=env,
        check=True,
    )

    # 4. Configure Git to use gh as the credential helper
    subprocess.run(["gh", "auth", "setup-git"], env=env, check=True)

    log("Git credentials store successfully refreshed from Token Broker! Token cached.")
    return token


def main():
    try:
        target_repo = sys.argv[1] if len(sys.argv) > 1 else None
        refresh_git_credentials(target_repo)
    except Exception as e:
        log(f"FATAL: Failed to refresh git credentials: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
