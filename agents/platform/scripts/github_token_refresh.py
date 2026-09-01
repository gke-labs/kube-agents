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
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

# Add scripts directory so gitops_workspace is importable
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from credential_proxy_client import authorization_headers


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [SRE-AUTH] {msg}", file=sys.stderr, flush=True)


TOKEN_BROKER_URL = os.getenv(
    "TOKEN_BROKER_URL",
    "http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token",
)


# Hosts this refresher will mint a token for. `ssh.github.com` is GitHub's
# SSH-over-443 endpoint and `www.github.com` is the redirecting alias `git
# clone` accepts, both naming the same repositories as `github.com`. An
# enterprise host is deliberately absent: Minty issues tokens for github.com
# installations only.
GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "ssh.github.com"})

# scp-like remote syntax — `[user@]host:path` — which is not a URL and so has
# to be split before the host can be compared.
_SCP_REMOTE = re.compile(r"^(?:[^/@]+@)?(?P<host>[^/:]+):(?P<path>.+)$")

# One `owner/name` slug, the shape `credential_proxy.is_valid_repository`
# accepts on the sidecar path. Checked here because the other path — direct to
# Minty, for the standalone deployments the module docstring names — has no
# validator downstream, so a deep link's extra segments would be posted as a
# repository name.
_REPOSITORY_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")


def github_repo_from_remote(url: str) -> str | None:
    """Return `owner/repo` when `url` is a GitHub remote, else None.

    The host is compared against `GITHUB_HOSTS` after parsing rather than
    searched for in the raw string: `https://evil.example/github.com/o/r.git`
    and `https://github.com.evil.example/o/r.git` both contain `github.com`,
    and a substring check would hand a token request for someone else's
    repository to Minty.
    """
    if "://" in url:
        parts = urlsplit(url)
        host, path = parts.hostname, parts.path
    else:
        match = _SCP_REMOTE.match(url)
        if not match:
            return None
        host, path = match.group("host"), match.group("path")

    if not host:
        return None
    if host.lower() not in GITHUB_HOSTS:
        # The host, never the URL: a remote can carry `user:token@` in front of
        # it. Without this an operator whose clone uses an SSH host alias sees
        # only the caller's "Could not identify target repository 'None'".
        log(f"Ignoring git remote: host '{host}' is not a GitHub host.")
        return None

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    owner, slash, name = path.partition("/")
    if (
        not slash
        or not _valid_repository_segment(owner)
        or not _valid_repository_segment(name)
    ):
        log(f"Ignoring git remote: path '{path}' is not an owner/repo slug.")
        return None
    return f"{owner}/{name}"


def _valid_repository_segment(segment: str) -> bool:
    """One half of an `owner/name` slug, with the traversal shapes rejected.

    The character class permits `.` and `-`, so it matches `..` and a leading
    dash as happily as a real name; neither is a repository.
    """
    return (
        _REPOSITORY_SEGMENT.fullmatch(segment) is not None
        and segment not in (".", "..")
        and not segment.startswith("-")
    )


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

    if not repository or repository.count("/") != 1:
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
