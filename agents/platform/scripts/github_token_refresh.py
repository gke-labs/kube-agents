#!/usr/bin/env python3
"""
GKE Platform Agent — Secure GitHub Token Refresher (Broker Client)

In the agent sandbox this script asks the credential sidecar to refresh. Only
the sidecar queries the token broker (Minty) directly. Standalone/legacy
deployments continue to use the direct path.
"""

import argparse
import email.message
import http.client
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Sequence

# Add scripts directory so gitops_workspace is importable
# Off when this file is the trusted copy -- see the same block in vcs_client.py.
TRUSTED_CLOSURE = "/opt/vcs/libexec/platform"
if not str(Path(__file__).resolve()).startswith(TRUSTED_CLOSURE + "/"):
    sys.path.append("/opt/defaults/scripts")
    sys.path.append("/opt/data/scripts")
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Ship alongside this script in the same directory, which is sys.path[0] both
# when the shell runs it and when the credential proxy execs it by absolute path.
import repo_ref  # noqa: E402 — needs the sys.path lines above
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

#: The two Minty scopes the rule ConfigMap declares (charts/kube-agents/templates/
#: github-minter.yaml). The write scope is what every managed repository rides;
#: the read scope grants `contents: read` alone and is minted per clone of a
#: context repository, by the broker, for its own git and nothing else.
MINTY_WRITE_SCOPE = "platform-agent-scope"
MINTY_READ_SCOPE = "platform-agent-read-scope"

#: The flag that selects the read-only mint. `credential_proxy.CommandExecutor`
#: spells the same flag when it runs this script; it cannot import this module
#: for the constant without also importing the CLI side, so the two copies are
#: kept in step by `test_credential_proxy.py`.
READ_ONLY_FLAG = "--read-only"

#: How long one Minty request may take, how long the CLI may take to install
#: what Minty returned, and how long gcloud may take to print an identity token.
MINTY_REQUEST_TIMEOUT_SECONDS = 5
CLI_SETUP_TIMEOUT_SECONDS = 15
GCLOUD_TIMEOUT_SECONDS = 5

#: Under a metadata-server identity the token gcloud prints comes from this
#: endpoint, so it is asked directly: one GET answered in tens of milliseconds,
#: where a cold gcloud process on a busy pod has taken longer than its five
#: seconds and failed the refresh (#2175). `format=full` is what google-auth
#: sends for gcloud, so the token is the same one. The address rather than
#: `metadata.google.internal`, so the timeout covers the whole attempt: a name
#: lookup has no bound of its own and walks the pod's search list when cluster
#: DNS is down. A slow answer is retried once; a refused connection, an error
#: status or an empty body falls through at once.
METADATA_IDENTITY_URL = (
    "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/identity"
)
METADATA_FLAVOR_HEADER = {"Metadata-Flavor": "Google"}
METADATA_TOKEN_FORMAT = "full"
METADATA_TIMEOUT_SECONDS = 3
METADATA_MAX_ATTEMPTS = 2
METADATA_RETRY_DELAY_SECONDS = 0.5
#: An identity token is a JWT of about a kilobyte; anything else at the
#: address (an error page, a proxy's answer) is not a token and falls through.
#: Each attempt runs under a wall-clock bound in a worker thread, because the
#: socket timeout bounds each connect and receive and not the whole answer.
METADATA_TOKEN_MAX_BYTES = 8192
METADATA_READ_CHUNK_BYTES = 1024
#: The shape the proxy's redactor uses for the same token: a JWT header is a
#: JSON object, so its base64url begins `eyJ`, and no segment is short.
JWT_SHAPE = re.compile(r"^eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")
#: A credential file named by either of these is the identity gcloud presents,
#: and it need not be the instance's: the metadata server is asked only when
#: neither names one, so a host that pointed gcloud at a key keeps minting as
#: that account. (The federated placement names its external_account file
#: here and is served by fetch_identity_token before this is consulted.)
CREDENTIAL_FILE_VARIABLES = ("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "GOOGLE_APPLICATION_CREDENTIALS")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx from the metadata address is an error, not a hop to follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


#: Opened without a proxy and without following redirects: the broker forwards
#: HTTP_PROXY into this helper for its GitHub and Minty traffic, and a
#: link-local metadata address must never be sent through it or lead anywhere
#: else. Module-level so a test can stand in for it.
metadata_open = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open

#: How the broker identity token was minted on this run, for the one line the
#: refresh writes last: the sidecar logs the tail of this helper's output, so
#: the branch and its duration have to be on the last line to be read.
identity_note = {"minted": "", "fell_through": ""}

#: The retry policy for one Minty request, shared by the write and the read-only
#: mint: three attempts, half a second before the second, doubling after.
#: Bounded so the whole mint fits inside the broker's executor timeout.
MINTY_MAX_ATTEMPTS = 3
MINTY_INITIAL_DELAY_SECONDS = 0.5
MINTY_BACKOFF_FACTOR = 2.0

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

    A remote has to *state* its host, so both shorthands `repo_ref` accepts are
    refused here: the bare `acme/repo`, which parses to no host at all, and
    `github.com/acme/repo`, which parses to an inferred one. Git produces
    neither. It will take the second — `git remote add origin
    github.com/acme/toolkit` succeeds — but as a relative local path, and
    reading it as a clone URL is what lets a directory name in a `.git/config`
    the sandbox writes stand in for a repository. `repo_ref.host_stated` is the
    distinction; the lift that supplies the other kind is for a registration,
    which is a value a person configured rather than one git emitted.

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
    if not ref.host_stated:
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


def describe_failure(exc: BaseException) -> str:
    """A subprocess failure with what the command printed, not only its exit code.

    `str(CalledProcessError)` is the exit status alone and `str(TimeoutExpired)`
    the timeout alone; the stderr both carry is what says why, and it was being
    dropped from every refresh failure the sidecar logged.
    """
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    detail = (stderr or "").strip()
    return f"{exc}: {detail}" if detail else str(exc)


def metadata_identity_token(audience: str) -> str | None:
    """The identity token for `audience` from the metadata server, or None.

    None when the metadata server is not the identity here (refused, an error
    status, a body that is not a JWT), and after `METADATA_MAX_ATTEMPTS` slow
    answers; the caller falls through to gcloud either way, and the reason is
    logged. Only a slow answer is retried: the others are a definite no.
    """
    query = urllib.parse.urlencode({"audience": audience, "format": METADATA_TOKEN_FORMAT})
    request = urllib.request.Request(
        f"{METADATA_IDENTITY_URL}?{query}", headers=METADATA_FLAVOR_HEADER
    )
    reason = ""
    # Timed as one step, retry included, so the line says what the refresh
    # spent here and not only what the last attempt did.
    started = time.monotonic()
    for attempt in range(1, METADATA_MAX_ATTEMPTS + 1):
        try:
            token = _bounded_attempt(request)
            if not token:
                reason = f"empty token after {time.monotonic() - started:.2f}s"
                break
            if not JWT_SHAPE.fullmatch(token):
                reason = f"body is not a JWT ({len(token)} chars) after {time.monotonic() - started:.2f}s"
                break
            how = f"from the metadata server in {time.monotonic() - started:.2f}s" + (
                f" (attempt {attempt})" if attempt > 1 else ""
            )
            identity_note["minted"] = how
            log(f"Minted the broker OIDC token {how}.")
            return token
        # URLError covers a refused connection, an error status and a 3xx,
        # OSError a socket timeout, HTTPException a body cut short, ValueError
        # a body that is not UTF-8 or too large. urlopen wraps a timeout raised
        # while connecting in URLError's `reason`; an attempt that outlives
        # its bound is a TimeoutError from _bounded_attempt.
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            reason = f"{exc} after {time.monotonic() - started:.2f}s"
            slow = isinstance(exc, TimeoutError) or isinstance(
                getattr(exc, "reason", None), TimeoutError
            )
            if not slow:
                break
            if attempt < METADATA_MAX_ATTEMPTS:
                log(f"WARNING: metadata server identity token attempt {attempt} timed out; retrying.")
        if attempt < METADATA_MAX_ATTEMPTS:
            time.sleep(METADATA_RETRY_DELAY_SECONDS)
    identity_note["fell_through"] = f"the metadata server gave none: {reason}"
    log(f"WARNING: no identity token from the metadata server ({reason}); asking gcloud.")
    return None


def _bounded_attempt(request) -> str:
    """One GET and its body under `METADATA_TIMEOUT_SECONDS` of wall clock.

    The socket timeout bounds each connect and each receive, so a peer that is
    slow between them (a header byte at a time) is never timed out by it; the
    attempt runs in a worker thread and is given up on, as a slow answer, when
    it outlives the bound. The thread then ends on its own next socket timeout,
    which is well inside this short-lived helper's life.
    """
    outcome = {}

    def work():
        try:
            with metadata_open(request, timeout=METADATA_TIMEOUT_SECONDS) as response:
                outcome["body"] = _read_token_body(response)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the caller's thread
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="metadata-identity-token", daemon=True)
    worker.start()
    worker.join(METADATA_TIMEOUT_SECONDS)
    if worker.is_alive():
        raise TimeoutError(f"attempt still running after {METADATA_TIMEOUT_SECONDS}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["body"]


def _read_token_body(response) -> str:
    """The response body as text, bounded in size and complete.

    Read one receive at a time so a body that grows past a token's size is cut
    off there, and refused when a Content-Length answer closed before it was
    complete: read1 returns b"" at EOF without raising, and a token cut inside
    its signature would otherwise pass the shape check and be sent to Minty.
    """
    chunks = []
    size = 0
    while True:
        chunk = response.read1(METADATA_READ_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > METADATA_TOKEN_MAX_BYTES:
            raise ValueError(f"body larger than a token (over {METADATA_TOKEN_MAX_BYTES} bytes)")
    if response.length:
        raise http.client.IncompleteRead(b"".join(chunks), response.length)
    return b"".join(chunks).decode("utf-8").strip()


def gcloud_identity_token(audience: str) -> str:
    """The identity token from `gcloud auth print-identity-token`, or raise.

    With `--audiences` first, then without it for a credential that refuses
    the flag. Each failure is kept with its stderr, so a timeout reads as a
    timeout and a refusal as what gcloud said.
    """
    started = time.monotonic()
    failures = []
    for argv in (
        ["gcloud", "auth", "print-identity-token", f"--audiences={audience}"],
        ["gcloud", "auth", "print-identity-token"],
    ):
        try:
            res = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=True,
                timeout=GCLOUD_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 -- every failure is reported, with its stderr
            failures.append(describe_failure(exc))
            continue
        oidc_token = res.stdout.strip()
        if not oidc_token:
            raise RuntimeError("Retrieved Google OIDC token via gcloud is empty.")
        how = f"through gcloud in {time.monotonic() - started:.2f}s"
        if identity_note["fell_through"]:
            how += f" ({identity_note['fell_through']})"
        identity_note["minted"] = how
        log(f"Minted the broker OIDC token {how}.")
        return oidc_token
    raise RuntimeError(
        "Failed to retrieve Google OIDC token via gcloud "
        f"after {time.monotonic() - started:.2f}s: " + "; ".join(failures)
    )


def broker_oidc_token() -> str:
    """The Google OIDC identity token Minty authenticates the caller by.

    Federation first, and only when the container is actually running on a
    federated credential -- fetch_identity_token returns None otherwise. The
    federated branch exists because gcloud refuses to mint an ID token from an
    external_account credential at all, so without it the co-located proxy can
    reach GCP but not GitHub. Then the metadata server directly, which is what
    every other placement runs on; gcloud last, for a host with neither.
    """
    oidc_token = wif_credentials.fetch_identity_token(TOKEN_BROKER_URL)
    if oidc_token:
        identity_note["minted"] = "through Workload Identity Federation"
        log("Minted the broker OIDC token through Workload Identity Federation.")
        return oidc_token
    configured = next((name for name in CREDENTIAL_FILE_VARIABLES if os.environ.get(name)), "")
    if configured:
        # Whatever that file names is the identity, not the instance's; only
        # gcloud reads it.
        identity_note["fell_through"] = f"{configured} names a credential file"
        log(f"{configured} names a credential file; asking gcloud for the identity token.")
        return gcloud_identity_token(TOKEN_BROKER_URL)
    return metadata_identity_token(TOKEN_BROKER_URL) or gcloud_identity_token(TOKEN_BROKER_URL)


def request_minty_token(
    oidc_token: str,
    org_name: str,
    repositories: Sequence[str],
    scope: str,
    *,
    max_attempts: int = MINTY_MAX_ATTEMPTS,
    initial_delay: float = MINTY_INITIAL_DELAY_SECONDS,
    backoff_factor: float = MINTY_BACKOFF_FACTOR,
) -> str:
    """One installation token from Minty for `repositories` under `scope`, with bounded retries."""
    headers = {"Content-Type": "application/json", "X-OIDC-Token": oidc_token}
    body = {
        "org_name": org_name,
        "repositories": list(repositories),
        "scope": scope,
    }
    req_data = json.dumps(body).encode("utf-8")

    log(
        f"Requesting scoped installation token from Minty for organization {org_name} "
        f"(repositories: {list(repositories)}, scope: {scope})..."
    )

    token = None
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            req = urllib.request.Request(
                TOKEN_BROKER_URL, data=req_data, headers=headers, method="POST"
            )
            with urllib.request.urlopen(
                req, timeout=MINTY_REQUEST_TIMEOUT_SECONDS
            ) as response:
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
    return token


def mint_read_only_token(target_repo: str | None) -> str:
    """A `contents: read` installation token for one repository, and nothing else.

    The credential the broker presents to its own `git clone` of a context
    repository (`content_workspace.py`, through `MintedReadCredential`). Three
    things distinguish it from `refresh_git_credentials`, each deliberate:

    * The scope is `MINTY_READ_SCOPE` and the repository list is this one
      repository. The write scope's list is widened to every managed repository
      so one token slot serves them all; a read token is per clone, so there is
      no slot to serve and no reason to widen.
    * It goes straight to Minty. There is no sidecar to delegate to, because
      this runs *in* the sidecar, and no sandbox to forward through, because the
      sandbox must never hold it.
    * It touches nothing: no CLI login, no credential helper, no file. The token
      is returned to the caller, which puts it in one git process's environment
      and lets it die with that process. The ambient write credential the CLI
      installed stays exactly as it was.
    """
    repository = target_repo.strip().strip("/") if target_repo else ""
    if not repo_ref.is_github_slug(repository):
        raise RuntimeError(
            f"Could not identify target repository '{repository}'. Must be in 'owner/repo' format."
        )
    org_name, repo_name = repository.split("/", 1)
    token = request_minty_token(
        broker_oidc_token(), org_name, [repo_name], MINTY_READ_SCOPE
    )
    log(f"Minted a read-only installation token for repository: {repository}")
    return token


class RefreshToken(str):
    """An installation token that carries the repositories it was scoped for."""

    scoped_repositories: tuple[str, ...]

    def __new__(
        cls,
        token: str,
        scoped_repositories: tuple[str, ...] | list[str] = (),
    ) -> "RefreshToken":
        obj = super().__new__(cls, token)
        obj.scoped_repositories = tuple(scoped_repositories)
        return obj


def refresh_git_credentials(
    target_repo: str | None = None,
    *,
    max_attempts: int = MINTY_MAX_ATTEMPTS,
    initial_delay: float = MINTY_INITIAL_DELAY_SECONDS,
    backoff_factor: float = MINTY_BACKOFF_FACTOR,
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
        # The forge-neutral route, naming the provider and the repository by
        # its URL rather than as a bare slug: a broker serving more than one
        # forge refuses a name without a host. `/v1/github/refresh` is kept on
        # the broker as an alias for agent images older than this.
        url = proxy_url.rstrip("/") + "/v1/forge/refresh"
        request = urllib.request.Request(
            url,
            data=json.dumps(
                {
                    "provider": "github",
                    "repository": f"https://{repo_ref.GITHUB_CANONICAL_HOST}/{repository}",
                }
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
                    return RefreshToken("", ())
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
    # and once the gateway holds no credential, CREDENTIAL_PROXY_URL is unset
    # there and the only route is this one. Putting the variable back on the
    # gateway would restore a credential path to the pod the split exists to
    # empty. Taking the route the model's shell already takes does not. The
    # direct mint below is the broker container's own path and the standalone
    # placement's; on the gateway, this forward runs before it is reached.
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
        return RefreshToken("", ())

    oidc_token = broker_oidc_token()

    # In a multi-repo deployment, scope the installation token to all managed
    # repositories within this organization to avoid pod-wide token slot churn.
    org_name, repo_name = repository.split("/", 1)
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

    token = request_minty_token(
        oidc_token,
        org_name,
        repositories_to_scope,
        MINTY_WRITE_SCOPE,
        max_attempts=max_attempts,
        initial_delay=initial_delay,
        backoff_factor=backoff_factor,
    )

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
            timeout=CLI_SETUP_TIMEOUT_SECONDS,
            env=env,
        )
        # Stop rewriting the config on every refresh. The helper `gh auth setup-git`
        # installs is static (!.../gh auth git-credential) and carries nothing token-specific.
        # Skipping the call when credential.https://github.com.helper already has it
        # removes the steady-state write, eliminating the .gitconfig lock collision window.
        configured_helper = subprocess.run(
            [
                "git",
                "config",
                "--global",
                "--get-all",
                "credential.https://github.com.helper",
            ],
            capture_output=True,
            text=True,
            timeout=CLI_SETUP_TIMEOUT_SECONDS,
            env=env,
        )
        if (
            configured_helper.returncode != 0
            or "gh auth git-credential" not in (configured_helper.stdout or "")
        ):
            subprocess.run(
                ["gh", "auth", "setup-git"],
                check=True,
                capture_output=True,
                text=True,
                timeout=CLI_SETUP_TIMEOUT_SECONDS,
                env=env,
            )
        # The identity note again on the last line: the sidecar keeps the tail
        # of this output, and a long managed-repository list in the Minty line
        # above would otherwise push the branch that minted out of its log.
        log(
            f"GitHub authentication successfully configured for repository: {repository}"
            + (f" (identity token {identity_note['minted']})" if identity_note["minted"] else "")
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or "").strip()
        detail_msg = f": {detail}" if detail else ""
        raise RuntimeError(
            f"Failed to configure GitHub auth in gh CLI: {e}{detail_msg}"
        ) from e
    except Exception as e:
        # With gh's stderr: the exit status alone cannot tell a token GitHub
        # rejected from a config file gh could not write.
        raise RuntimeError(
            f"Failed to configure GitHub auth in gh CLI: {describe_failure(e)}"
        ) from e

    scoped = tuple(f"{org_name}/{r}".lower() for r in repositories_to_scope)
    return RefreshToken(token, scoped)


def main():
    parser = argparse.ArgumentParser(
        description="Refresh the GitHub credential the CLI and git ride, or "
        "mint a read-only token for one repository."
    )
    parser.add_argument("repository", nargs="?", help="owner/repo to mint for")
    parser.add_argument(
        READ_ONLY_FLAG,
        action="store_true",
        help="print a contents:read installation token for the repository on "
        "stdout, installing nothing; the broker's clone of a context repository",
    )
    args = parser.parse_args()
    try:
        if args.read_only:
            print(mint_read_only_token(args.repository), end="")
            return
        token = refresh_git_credentials(args.repository)
        scoped = getattr(token, "scoped_repositories", None)
        if isinstance(scoped, (list, tuple, set, frozenset)):
            for r in scoped:
                print(r)
    except Exception as e:
        log(f"FATAL: Failed to refresh git credentials: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
