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

# Add scripts directory so gitops_workspace is importable
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Ship alongside this script in the same directory, which is sys.path[0] both
# when the shell runs it and when the credential proxy execs it by absolute path.
import forge_clis  # noqa: E402 — needs the sys.path lines above
import repo_ref  # noqa: E402 — needs the sys.path lines above
import wif_credentials  # noqa: E402 — needs the sys.path lines above
from credential_proxy_client import authorization_headers  # noqa: E402

#: Must match `credential_proxy.FORGE_REFRESH_PATH`. Not imported from it: that
#: module runs in the credential sidecar and this one runs in the agent sandbox,
#: and the point of the split is that the sandbox does not load the broker.
FORGE_REFRESH_PATH = "/v1/forge/refresh"

#: The route a sidecar built before the rename serves, and the only one it
#: serves. Tried after the one above when that answers 404 — see the fallback in
#: `refresh_git_credentials` for why that is safe and why it is GitHub-only.
LEGACY_GITHUB_REFRESH_PATH = "/v1/github/refresh"

#: The status an HTTP server returns for a route it does not have.
HTTP_NOT_FOUND = 404

#: The tool that both authenticates to GitHub and, via `auth setup-git`,
#: installs itself as git's credential helper. Taken from the table the sidecar
#: enforces so the binary this script shells is one the allowlist admits.
GITHUB_CLI = forge_clis.FORGE_EXECUTABLES[forge_clis.GITHUB]


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
    provider: str = forge_clis.DEFAULT_FORGE_PROVIDER,
    max_attempts: int = 3,
    initial_delay: float = 0.5,
    backoff_factor: float = 2.0,
) -> str:
    """Query local Minty, retrieve token, and cache inside git credentials.

    `provider` names the forge. It travels to the sidecar in the request body
    on the brokered path, and selects the credential helper on the direct one.
    Defaulted rather than required because every existing caller means GitHub
    and this script's own name says so.
    """
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
        # `/v1/forge/refresh`, with the provider in the body.
        #
        # Both images are versioned independently — an operator may pin
        # `CREDENTIAL_PROXY_IMAGE` to a mirrored digest while the agent tracks a
        # tag — so each direction of skew has to survive on its own. The sidecar
        # still serving `/v1/github/refresh` covers an old agent against a new
        # sidecar; this fallback covers the reverse, where a sidecar that
        # predates the rename has no route here and answers 404. Without it
        # every git write path fails until the sidecar rolls.
        #
        # Strictly on 404, and strictly for GitHub. A sidecar that serves this
        # route answers 200, 400 or 502 and never 404, so no genuine refusal is
        # ever sent twice; and a sidecar old enough to lack the route is old
        # enough to serve GitHub alone, so retrying another provider's refresh
        # on the legacy path would mint a GitHub credential for a repository
        # that is not on GitHub.
        paths = [FORGE_REFRESH_PATH]
        if provider == forge_clis.GITHUB:
            paths.append(LEGACY_GITHUB_REFRESH_PATH)

        last_error: Exception | None = None
        for index, path in enumerate(paths):
            request = urllib.request.Request(
                proxy_url.rstrip("/") + path,
                data=json.dumps(
                    {"repository": repository, "provider": provider}
                ).encode("utf-8"),
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
                last_error = RuntimeError(
                    f"Credential sidecar failed to refresh GitHub auth: HTTP {exc.code}"
                )
                last_error.__cause__ = exc
                if exc.code == HTTP_NOT_FOUND and index + 1 < len(paths):
                    log(
                        f"Credential sidecar has no {path}; retrying on "
                        f"{paths[index + 1]} for an older sidecar."
                    )
                    continue
                raise last_error from exc
            except Exception as exc:
                raise RuntimeError(
                    f"Credential sidecar failed to refresh GitHub auth: {exc}"
                ) from exc

        raise last_error  # unreachable: the loop returns or raises on every path

    # Past this point the script is GitHub's token-acquisition strategy and
    # nothing else: Minty mints GitHub App installation tokens from a JWT signed
    # by the App's private key, and `gh auth setup-git` is what makes git use
    # them. A GitLab group access token needs no minting step, so it gets its own
    # refresher rather than a branch through this one. Refuse rather than
    # proceed: the alternative is asking Minty for a GitHub token in the name of
    # a repository that is not on GitHub.
    #
    # This guards the direct path only, and deliberately. Above it the script is
    # a transport — it forwards whatever provider it was given to the sidecar,
    # which dispatches to that provider's refresher through
    # `credential_proxy.FORGE_REFRESHERS` and refuses a provider the install is
    # not configured for. So in the agent sandbox, where `CREDENTIAL_PROXY_URL`
    # is always set, a non-GitHub provider is refused there rather than here.
    # Here is the standalone/legacy deployment, which has no sidecar to dispatch
    # and would otherwise fall straight into the Minty call below.
    if provider != forge_clis.GITHUB:
        raise RuntimeError(
            f"{Path(__file__).name} refreshes {forge_clis.GITHUB} credentials only; "
            f"asked for '{provider}'."
        )

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

    # 3. Configure gh CLI authentication and Git credentials.
    # `auth setup-git` writes `credential.helper = !gh auth git-credential`, so
    # the credential helper here is a forge's CLI rather than a username and a
    # URL template a provider could supply. That is why a forge with no CLI
    # cannot reuse this and needs a helper written for it.
    try:
        env = os.environ.copy()
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)
        subprocess.run(
            [GITHUB_CLI, "auth", "login", "--with-token"],
            input=token,
            text=True,
            check=True,
            capture_output=True,
            timeout=15,
            env=env,
        )
        subprocess.run(
            [GITHUB_CLI, "auth", "setup-git"],
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
