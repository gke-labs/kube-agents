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
from pathlib import Path

# Add scripts directory so gitops_workspace is importable
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import repo_ref  # noqa: E402  (needs the sys.path lines above)

from credential_proxy_client import authorization_headers


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [SRE-AUTH] {msg}", file=sys.stderr, flush=True)


TOKEN_BROKER_URL = os.getenv(
    "TOKEN_BROKER_URL",
    "http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token",
)


def github_repo_from_remote(url: str) -> str | None:
    """Return `owner/repo` when `url` is a GitHub remote, else None.

    A remote always names a host, so the bare shorthand is refused here even
    though `repo_ref` parses it: git cannot produce an `origin` of `acme/repo`,
    and accepting one would let a stray config value stand in for a clone URL.

    The host is compared after parsing rather than searched for in the raw
    string — `https://evil.example/github.com/o/r.git` and
    `https://github.com.evil.example/o/r.git` both contain `github.com`, and a
    substring check would hand a token request for someone else's repository to
    Minty. `repo_ref` is where that happens now; the log lines here are what
    keeps a refusal from surfacing only as the caller's "Could not identify
    target repository 'None'".
    """
    ref = repo_ref.try_parse(url)
    if ref is None:
        log(f"Ignoring git remote: '{url}' is not a repository URL.")
        return None
    if not ref.host:
        log(f"Ignoring git remote: '{url}' names no host.")
        return None
    if not ref.is_github:
        log(f"Ignoring git remote: host '{ref.host}' is not a GitHub host.")
        return None
    if len(ref.segments) != repo_ref.GITHUB_PATH_DEPTH:
        log(f"Ignoring git remote: path '{ref.path}' is not an owner/repo slug.")
        return None
    return ref.path


def get_current_git_repo(cwd: str | None = None) -> str | None:
    """Extract repository name (owner/repo) from local git config."""
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=True,
        )
        return github_repo_from_remote(res.stdout.strip())
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
    """Query local Minty, retrieve token, and cache inside git credentials."""
    repository = target_repo.strip().strip("/") if target_repo else get_current_git_repo()

    # The slash count this replaced counted separators in whatever it was
    # handed, so `github.com/acme` passed it. The other path out of here —
    # direct to Minty, for the standalone deployments the module docstring
    # names — has no validator downstream, so this is the last check before a
    # value is posted as a repository name.
    if not repo_ref.is_github_slug(repository):
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

    # In a multi-repo deployment, scope the installation token to all managed
    # repositories within this organization to avoid pod-wide token slot churn.
    repositories_to_scope = [repo_name]
    try:
        from gitops_workspace import get_managed_github_repos

        for m in get_managed_github_repos():
            if "/" in m:
                m_org, m_repo = m.split("/", 1)
                if (
                    m_org.lower() == org_name.lower()
                    and m_repo not in repositories_to_scope
                ):
                    repositories_to_scope.append(m_repo)
    except Exception as e:
        log(f"WARNING: Could not expand managed repositories for token scoping: {e}")

    headers = {"Content-Type": "application/json", "X-OIDC-Token": oidc_token}
    body = {
        "org_name": org_name,
        "repositories": repositories_to_scope,
        "scope": "platform-agent-scope",
    }
    req_data = json.dumps(body).encode("utf-8")

    log(
        f"Requesting scoped installation token from Minty for organization {org_name} (repositories: {repositories_to_scope})..."
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
