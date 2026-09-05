#!/usr/bin/env python3
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
from typing import Sequence
from urllib.parse import urlsplit

# Add scripts directory so gitops_workspace is importable
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Ships alongside this script in the same directory, which is sys.path[0] both
# when the shell runs it and when the credential proxy execs it by absolute path.
import wif_credentials  # noqa: E402 — needs the sys.path lines above
from credential_proxy_client import authorization_headers  # noqa: E402


def log(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [SRE-AUTH] {msg}", file=sys.stderr, flush=True)


TOKEN_BROKER_URL = os.getenv(
    "TOKEN_BROKER_URL",
    "http://github-token-minter.kubeagents-system.svc.cluster.local:8080/token",
)

#: Shell convention for "command not found", reused so a missing binary stays
#: distinguishable from a gh command that ran and failed.
GH_MISSING_RC = 127

#: The credential sidecar's own timeout (`_execute` in credential_proxy.py),
#: surfaced through credential_proxy_client. Excluded from the retry because a
#: command that ran for the full timeout may well have landed its write; see
#: looks_like_auth_failure.
GH_TIMEOUT_RC = 124

#: Where this same script lands in the shell sandbox. deploy/sandbox/entrypoint.sh
#: copies /opt/defaults/scripts into the machine home under /opt/data, so the path
#: resolves there and is the one refresh_git_credentials forwards to.
SANDBOX_REFRESH_SCRIPT = "/opt/data/scripts/github_token_refresh.py"

#: Bounds the ssh hop around that forward. The broker's own retry budget bounds
#: the work inside it; this is the 60s the in-pod HTTP branch allows plus room
#: for the connection.
SANDBOX_REFRESH_TIMEOUT_SECONDS = 90

# What `gh` prints when the credential is the problem, as opposed to the
# repository, the network, or the rate limit. Matched case-insensitively
# against stderr: the REST paths emit `HTTP 401: Bad credentials`, the GraphQL
# ones `requires authentication`, and `auth status` (which is handled
# separately, being the explicit question) `not logged in` / `token is invalid`.
_GH_AUTH_FAILURE = re.compile(
    r"HTTP 401"
    r"|bad credentials"
    r"|requires authentication"
    r"|authentication failed"
    r"|not logged in"
    r"|token is invalid"
    r"|invalid token",
    re.IGNORECASE,
)


def looks_like_auth_failure(args: Sequence[str] | list, result: subprocess.CompletedProcess) -> bool:
    """Does this failure look like one a fresh token would fix?

    The retry exists for an expired installation token, and minting on anything
    else spends a credential on a fault no credential can repair. `gh auth
    status` passes whenever *any* host is authenticated, so a repository the
    token cannot reach fails only at `issue list` with a 404 -- and gating the
    retry on ``returncode != 0`` alone turned that permanent misconfiguration
    into a mint on every ten-minute tick, indefinitely.
    """
    if result.returncode == 0:
        return False
    if result.returncode in (GH_MISSING_RC, GH_TIMEOUT_RC):
        return False
    if list(args)[:2] == ["auth", "status"]:
        return True
    return bool(_GH_AUTH_FAILURE.search(result.stderr or ""))


_refresh_attempted = False
_refresh_failed = False


def is_refresh_failed() -> bool:
    """True if a credential refresh was attempted during this process and failed."""
    return _refresh_failed


def reset_refresh_state() -> None:
    """Reset the at-most-once refresh guard and failure state (primarily for tests)."""
    global _refresh_attempted, _refresh_failed
    _refresh_attempted = False
    _refresh_failed = False


def refresh_credentials_once(
    args: Sequence[str] | None = None,
    *,
    repo: str | None = None,
) -> bool:
    """Mint a fresh token, at most once per process.

    Returns True only when a new token actually landed -- i.e. when retrying
    the gh command that just failed is worth doing.

    The at-most-once guard is what bounds the cost. Each entry point runs as
    its own invocation, so one invocation makes one mint however many gh calls
    it makes, and a credential broken for a reason no token fixes cannot turn a
    single poll into a mint per call.

    Note: In multi-org deployments, if an un-scoped preflight check (e.g. `auth
    status`) triggers token refresh, it mints for the first managed repository.
    Subsequent 401s for a repository in a different organization within the same
    process will not trigger a second mint due to the process-wide at-most-once
    guard. Full multi-org refresh across different organizations requires lifting
    the guard to once-per-organization.
    """
    global _refresh_attempted, _refresh_failed
    if _refresh_attempted:
        return False
    _refresh_attempted = True

    if not repo and args:
        argv_list = list(args)
        for flag in ("-R", "--repo"):
            if flag in argv_list:
                try:
                    repo = argv_list[argv_list.index(flag) + 1]
                    break
                except (ValueError, IndexError):
                    pass

    if not repo:
        try:
            from gitops_workspace import get_managed_github_repos
            managed = get_managed_github_repos()
            repo = managed[0] if managed else None
        except Exception:
            repo = None

    if not repo:
        return False

    try:
        refresh_git_credentials(repo)
    except Exception as exc:
        log(f"GitHub credential refresh failed: {type(exc).__name__}: {exc}")
        _refresh_failed = True
        return False
    return True


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

    # No CREDENTIAL_PROXY_URL, but a shell sandbox is configured: this is the
    # gateway pod, which holds nothing that can mint. Forward to the sandbox,
    # which has the variable and a route to the broker's Service.
    #
    # The `no_agent` cron jobs are what need this. They run as a plain Python
    # subprocess on the gateway rather than as a model turn, so they never touch
    # the terminal backend and never reach the sandbox the way a skill does —
    # and once the gateway holds no credential, both branches around this one
    # are dead there: CREDENTIAL_PROXY_URL is unset, and the direct mint below
    # ends at `No such file or directory: 'gcloud'`. Putting the variable back
    # on the gateway would fix it by restoring a credential path to the pod the
    # split exists to empty. Taking the route the model's shell already takes
    # does not.
    #
    # The forwarded process re-enters this function in the sandbox, where
    # CREDENTIAL_PROXY_URL is set, so it takes the branch above and stops.
    # There is no way round that into a loop: sandbox_enabled() reads the
    # gateway's managed Hermes config, which the sandbox image does not carry.
    try:
        import sandbox_exec
    except ImportError:
        sandbox_exec = None
    if sandbox_exec is not None and sandbox_exec.sandbox_enabled():
        # SandboxUnavailable is a RuntimeError and is deliberately not caught:
        # ssh failing to connect means the mint never ran, and this function's
        # contract is that a failure raises rather than returning quietly.
        completed = sandbox_exec.run(
            ["python3", SANDBOX_REFRESH_SCRIPT, repository],
            timeout=SANDBOX_REFRESH_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"the shell sandbox could not refresh GitHub auth for {repository} "
                f"(exit {completed.returncode}): {(completed.stderr or '').strip()}"
            )
        log(f"GitHub credentials refreshed through the shell sandbox for {repository}.")
        return ""

    # 1. Retrieve Google OIDC identity token.
    #
    # Federation first, and only when the container is actually running on a
    # federated credential -- fetch_identity_token returns None otherwise and
    # this falls through to the metadata server via gcloud, which is what every
    # placement other than the co-located sandbox proxy uses. The federated
    # branch exists because gcloud refuses to mint an ID token from an
    # external_account credential at all, so without it the co-located proxy can
    # reach GCP but not GitHub.
    oidc_token = wif_credentials.fetch_identity_token(TOKEN_BROKER_URL)
    if oidc_token:
        log("Minted the broker OIDC token through Workload Identity Federation.")
    else:
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
