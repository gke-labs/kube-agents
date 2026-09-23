#!/usr/bin/env python3
"""Close the platform agent's leftover pull requests in the pool's free projects.

A remediation scenario opens a pull request in the leased project's GitOps repo,
and nothing closed it. The next lease of that project meets its predecessor's:
`create_pull_request` in agents/platform/skills/submit-suggestion/scripts/
submit_suggestion.py treats "a pull request already exists" as success and
returns the old one's URL (#1755). The `pull_request_opened` check grades the
head commit, so an inherited pull request no longer passes as this run's work;
what it still costs is a repository that fills up, and a repetition reproducing
the same fix refused "nothing to commit" by the leftover branch.

Runs from a Prow periodic that executes only `main`, never a pull request's
code. That placement is the point: closing a pull request needs
`pull_requests: write` on every pool repository, and a presubmit runs the pull
request's own scripts, so a write credential mounted there is reachable by any
change under test. Here the credential is the agent's own GitHub App, signed
through each project's KMS key (the same key minty signs with in-cluster), by a
service account only this job runs as. The token is narrowed at mint to one
repository and `pull_requests: write`; the key never leaves KMS.

Which projects: the ones Boskos hands out as `free`. Each is acquired into a
`cleaning` state for the seconds the sweep takes and released back to `free`,
so a project a run holds is never touched, and a run arriving mid-sweep waits
those seconds at its own acquire. No listing endpoint is needed and no run's
state is read.

Three conditions, all required, matching is_agent_pull_request in
agents/platform/scripts/forge.py: authored by the agent's bot, head branch
carrying the agent's prefix, and that branch in the repository itself rather
than a fork. The branch is left: deleting a ref needs `contents: write`, which
this mint deliberately does not ask for.
"""

import argparse
import base64
import http.client
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "kube-agents-pull-sweep"
REQUEST_TIMEOUT_SECONDS = 30

# Must equal AGENT_BRANCH_PREFIX in agents/platform/scripts/forge.py, which is
# what names the branches these pull requests come from. A test pins the two.
AGENT_BRANCH_PREFIX = "platform-agent/"

# GitHub rejects an App JWT whose exp is more than ten minutes out; nine leaves
# room for clock skew, and the backdated iat covers a slow runner.
JWT_LIFETIME_SECONDS = 540
JWT_BACKDATE_SECONDS = 60

# The App this signs as is the one the agent submits with, so the author to
# look for is its own bot. hack/ci-deploy.sh hands the same App to the agent as
# EVAL_GITHUB_APP_ID; a test pins the slug to that script. Written out rather
# than read from GET /app so a sweep that cannot mint still reports a name.
BOT_LOGIN_SUFFIX = "[bot]"
AGENT_APP_SLUG = "kube-agents-evals-token-minter"
AGENT_BOT_LOGIN = AGENT_APP_SLUG + BOT_LOGIN_SUFFIX
DEFAULT_APP_ID = "4675512"

# The only permission this asks for. The installation carries contents and
# issues write as well, for the agent; a token that inherited it whole would
# hold both here for nothing.
TOKEN_PERMISSIONS = {"pull_requests": "write"}

PER_PAGE = 100
# A bound rather than a budget: orders of magnitude above any pool repository,
# so a paging bug cannot spin here for the job's whole window.
MAX_PAGES = 20

# What GitHub answers when the installation lacks a permission the mint asked
# for. Its message is about the token, which reads as a code fault; the usual
# cause is an installation whose permissions were narrowed. Not the only one --
# 403 also covers a suspended installation and a secondary rate limit -- so the
# error names all three rather than asserting the first.
PERMISSION_NOT_GRANTED_CODES = (403, 422)

# Where every pool project keeps the App's private key: the import-only signing
# key terraform/examples/ci-pool-minter creates and provision_ci_pool_project.sh
# imports the PEM into. Version 1 is what the chart pins for minty
# (charts/kube-agents/values.yaml, githubMinter.kms.keyVersion); the ring is
# regional and the pool is provisioned in one region (REGION in hack/ci-env.sh).
KMS_LOCATION = "us-central1"
KMS_KEYRING = "github-token-minter-keyring"
KMS_KEY = "github-token-minter-key"
KMS_KEY_VERSION = "1"
# RS256 is PKCS#1 v1.5 over SHA-256, which is the key's algorithm
# (RSA_SIGN_PKCS1_2048_SHA256); gcloud hashes the input with this and signs.
KMS_DIGEST_ALGORITHM = "sha256"
GCLOUD_TIMEOUT_SECONDS = 60
GCLOUD_ERROR_CHARS = 300

# The Prow wrapper's Boskos conventions (oss-test-infra
# prow/prowjobs/gke-labs/kube-agents/kube-agents-presubmits.yaml): the server
# inside the build cluster, the resource type, and the states. A project is
# held in BOSKOS_SWEEP_STATE only while its repository is being swept.
BOSKOS_DEFAULT_SERVER = "http://boskos.boskos.svc.cluster.local"
BOSKOS_RESOURCE_TYPE = "kube-agents-evals-project"
BOSKOS_FREE_STATE = "free"
BOSKOS_SWEEP_STATE = "cleaning"
BOSKOS_TIMEOUT_SECONDS = 30
# Boskos answers /acquire with 404 when no resource is in the requested state.
BOSKOS_NO_RESOURCE_CODE = 404
# Boskos picks any free resource, so after a release the same project can come
# straight back. Stop after this many consecutive repeats: the pool has been
# walked, and anything unvisited is busy.
BOSKOS_MAX_CONSECUTIVE_REPEATS = 3
DEFAULT_BOSKOS_OWNER = "ci-kube-agents-pull-sweep"
# A sweep killed mid-hold (deadline, node loss) would leave its project in
# BOSKOS_SWEEP_STATE: not free, not busy, unusable. Each run starts by asking
# Boskos to return anything that has sat there longer than this to free -- its
# own /reset, a Go duration -- so a strand outlives at most one interval. A
# sweep holds a project for seconds, so nothing live is inside the window.
BOSKOS_STRANDED_AFTER = "15m"
# Prow ends a job with SIGTERM and a grace period before SIGKILL. Python's
# default SIGTERM action skips `finally`, which is where a held project is
# released; converting it to an exception is what lets the release run.
TERMINATED_EXIT_CODE = 143

# The project-to-repository mapping keeps its one home in hack/ci-deploy.sh; a
# dozen documents and scripts read it out of that file. The lines are
# `    <project>) echo "<owner>/<repo>" ;;` inside gitops_repo_for_project().
CI_DEPLOY_SCRIPT = pathlib.Path(__file__).resolve().parent / "ci-deploy.sh"
MAPPING_FUNCTION = "gitops_repo_for_project"
MAPPING_LINE_RE = re.compile(r'^\s+([A-Za-z0-9-]+)\)\s+echo "([^"/]+/[^"]+)"\s+;;\s*$')


class SweepError(Exception):
    """A fault that stops one repository's sweep. The caller reports it."""


class Terminated(Exception):
    """SIGTERM arrived; the run unwinds, releasing what it holds."""


def _terminate(signum, frame):
    raise Terminated("signal %d" % signum)


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def kms_sign(project, signing_input, runner=subprocess.run):
    """PKCS#1 v1.5 SHA-256 signature over `signing_input`, by the project's key.

    gcloud rather than the KMS REST API: the periodic's image is the Cloud SDK,
    Workload Identity is already wired for it, and the CLI hashes and signs in
    one call. `runner` is a seam for the tests.
    """
    with tempfile.TemporaryDirectory() as scratch:
        input_path = os.path.join(scratch, "signing-input")
        signature_path = os.path.join(scratch, "signature")
        with open(input_path, "wb") as handle:
            handle.write(signing_input)
        signed = runner(
            [
                "gcloud",
                "kms",
                "asymmetric-sign",
                "--project=%s" % project,
                "--location=%s" % KMS_LOCATION,
                "--keyring=%s" % KMS_KEYRING,
                "--key=%s" % KMS_KEY,
                "--version=%s" % KMS_KEY_VERSION,
                "--digest-algorithm=%s" % KMS_DIGEST_ALGORITHM,
                "--input-file=%s" % input_path,
                "--signature-file=%s" % signature_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GCLOUD_TIMEOUT_SECONDS,
        )
        if signed.returncode != 0:
            raise SweepError(
                "gcloud could not sign with %s's %s: %s"
                % (project, KMS_KEY, signed.stderr.decode()[:GCLOUD_ERROR_CHARS])
            )
        try:
            with open(signature_path, "rb") as handle:
                signature = handle.read()
        except OSError as exc:
            raise SweepError("gcloud wrote no signature for %s: %s" % (project, exc))
    if not signature:
        raise SweepError("gcloud wrote an empty signature for %s" % project)
    return signature


def app_jwt(app_id, project, runner=subprocess.run):
    """An App JWT signed by the copy of the key in `project`'s KMS."""
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64(
        json.dumps(
            {
                "iat": now - JWT_BACKDATE_SECONDS,
                "exp": now + JWT_LIFETIME_SECONDS,
                "iss": str(app_id),
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = header + b"." + payload
    return (signing_input + b"." + _b64(kms_sign(project, signing_input, runner))).decode("ascii")


def api(method, path, authorization, body=None):
    """One GitHub call. Returns the decoded body, or None when it is empty."""
    data = None
    headers = {
        "Authorization": authorization,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": USER_AGENT,
    }
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(API_ROOT + path, method=method, headers=headers, data=data)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def open_pulls(repo, authorization):
    """Every open pull request in the repository, by page."""
    found = []
    for page in range(1, MAX_PAGES + 1):
        batch = api(
            "GET",
            "/repos/%s/pulls?state=open&per_page=%d&page=%d" % (repo, PER_PAGE, page),
            authorization,
        )
        if not batch:
            break
        found.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return found


def is_agent_pull_request(pull, repo, bot_login):
    """The ownership test from forge.py, over a REST pull-request object.

    The branch prefix alone is not ownership -- anyone who can fork can name a
    branch with it -- so the author and the head repository are checked too.
    """
    head = pull.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name") or ""
    author = (pull.get("user") or {}).get("login") or ""
    return (
        author.lower() == bot_login.lower()
        and str(head.get("ref") or "").startswith(AGENT_BRANCH_PREFIX)
        and head_repo.lower() == repo.lower()
    )


def scoped_token(app_id, project, repo, runner=subprocess.run):
    """A token for this repository alone, signed with this project's key.

    The installation is resolved from the repository rather than passed in: one
    App serves the whole pool, and a hardcoded id is a silent 404 on every
    repository but one. The token is narrowed twice -- to this one repository,
    and to TOKEN_PERMISSIONS -- so the sweep never holds the reach the App has.
    """
    bearer = "Bearer " + app_jwt(app_id, project, runner)
    try:
        installation = api("GET", "/repos/%s/installation" % repo, bearer)["id"]
    except urllib.error.HTTPError as exc:
        # 401: the key in this project's KMS is not App app_id's. 404: the App
        # is not installed on this repository, an onboarding gap rather than a
        # fault here.
        raise SweepError(
            "GitHub answered HTTP %d (%s) locating App %s's installation on %s"
            % (exc.code, exc.reason, app_id, repo)
        )
    try:
        minted = api(
            "POST",
            "/app/installations/%s/access_tokens" % installation,
            bearer,
            {"repositories": [repo.split("/")[-1]], "permissions": dict(TOKEN_PERMISSIONS)},
        )
    except urllib.error.HTTPError as exc:
        if exc.code in PERMISSION_NOT_GRANTED_CODES:
            raise SweepError(
                "App %s cannot mint %s on %s (HTTP %d). Usually the installation no longer "
                "holds the permission, and an organisation owner restores it in settings; the "
                "same code also answers a suspended installation and a secondary rate limit."
                % (app_id, sorted(TOKEN_PERMISSIONS), repo, exc.code)
            )
        raise SweepError(
            "GitHub answered HTTP %d (%s) minting for App %s on %s"
            % (exc.code, exc.reason, app_id, repo)
        )
    return minted["token"]


def close_agent_pulls(repo, authorization, dry_run=False):
    """Close every open pull request the agent owns. Returns (closed, unclosed)."""
    closed = 0
    unclosed = []
    for pull in open_pulls(repo, authorization):
        if not is_agent_pull_request(pull, repo, AGENT_BOT_LOGIN):
            continue
        number = pull["number"]
        print("  #%s (%s)" % (number, pull["head"]["ref"]))
        if dry_run:
            closed += 1
            continue
        # Each close stands alone. One that fails is reported and the sweep
        # carries on: giving up here would leave every later pull request open,
        # which is the thing being fixed. HTTPException covers a response cut
        # short mid-read, which urllib does not raise as OSError.
        try:
            api(
                "PATCH",
                "/repos/%s/pulls/%s" % (repo, number),
                authorization,
                {"state": "closed"},
            )
        except (urllib.error.HTTPError, OSError, http.client.HTTPException) as exc:
            print("  #%s did not close (%s)" % (number, exc), file=sys.stderr)
            unclosed.append(number)
            continue
        closed += 1
    return closed, unclosed


def sweep_repo(project, repo, app_id, dry_run=False, runner=subprocess.run):
    """Close the agent's leftovers in one project's repository; returns the count."""
    authorization = "token " + scoped_token(app_id, project, repo, runner)
    closed, unclosed = close_agent_pulls(repo, authorization, dry_run=dry_run)
    print(
        "%s %d pull request(s) by %s in %s"
        % ("would close" if dry_run else "closed", closed, AGENT_BOT_LOGIN, repo)
    )
    if unclosed:
        raise SweepError(
            "left %d pull request(s) open in %s: %s"
            % (len(unclosed), repo, ", ".join("#%s" % n for n in unclosed))
        )
    return closed


def pool_repos(ci_deploy_script=CI_DEPLOY_SCRIPT):
    """{project: owner/repo} from gitops_repo_for_project() in hack/ci-deploy.sh."""
    mapping = {}
    inside = False
    for line in pathlib.Path(ci_deploy_script).read_text(encoding="utf-8").splitlines():
        if line.startswith(MAPPING_FUNCTION + "()"):
            inside = True
            continue
        if inside and line.startswith("}"):
            break
        if inside:
            match = MAPPING_LINE_RE.match(line)
            if match:
                mapping[match.group(1)] = match.group(2)
    if not mapping:
        raise SweepError("no %s() mapping found in %s" % (MAPPING_FUNCTION, ci_deploy_script))
    return mapping


def _boskos(server, action, params):
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        "%s/%s?%s" % (server.rstrip("/"), action, query), method="POST"
    )
    with urllib.request.urlopen(request, timeout=BOSKOS_TIMEOUT_SECONDS) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def boskos_acquire(server, owner):
    """One free project moved to the sweep state, or None when there is none."""
    try:
        resource = _boskos(
            server,
            "acquire",
            {
                "type": BOSKOS_RESOURCE_TYPE,
                "state": BOSKOS_FREE_STATE,
                "dest": BOSKOS_SWEEP_STATE,
                "owner": owner,
            },
        )
    except urllib.error.HTTPError as exc:
        if exc.code == BOSKOS_NO_RESOURCE_CODE:
            return None
        raise
    return (resource or {}).get("name") or None


def boskos_release(server, owner, name):
    _boskos(server, "release", {"name": name, "dest": BOSKOS_FREE_STATE, "owner": owner})


def boskos_reset_stranded(server):
    """Projects an earlier sweep left in the sweep state, returned to free.

    Returns their names. A failure here is reported and does not stop the
    sweep: the projects it would have freed stay where they are until the next
    run, which is no worse than not having asked.
    """
    try:
        stranded = _boskos(
            server,
            "reset",
            {
                "type": BOSKOS_RESOURCE_TYPE,
                "state": BOSKOS_SWEEP_STATE,
                "dest": BOSKOS_FREE_STATE,
                "expire": BOSKOS_STRANDED_AFTER,
            },
        )
    except (urllib.error.HTTPError, OSError, http.client.HTTPException) as exc:
        print("could not reset stranded projects: %s" % exc, file=sys.stderr)
        return []
    names = sorted(stranded or {})
    for name in names:
        print("returned %s to free: left in %s by an earlier sweep" % (name, BOSKOS_SWEEP_STATE))
    return names


def sweep_pool(server, owner, app_id, mapping, dry_run=False, runner=subprocess.run):
    """Sweep every project Boskos will hand out as free, once each.

    Returns (closed_by_project, failures_by_project, unmapped). A project is
    released in every path, including a fault mid-sweep; holding one would take
    it out of the pool until a human noticed.
    """
    boskos_reset_stranded(server)
    closed = {}
    failures = {}
    unmapped = []
    visited = set()
    repeats = 0
    # Bounded twice: by consecutive repeats, and by an absolute count no pool
    # can reach, so a Boskos that keeps answering cannot hold the job open.
    for _ in range(2 * len(mapping) + BOSKOS_MAX_CONSECUTIVE_REPEATS):
        name = boskos_acquire(server, owner)
        if name is None:
            break
        try:
            if name in visited:
                repeats += 1
                if repeats >= BOSKOS_MAX_CONSECUTIVE_REPEATS:
                    break
                continue
            repeats = 0
            visited.add(name)
            repo = mapping.get(name)
            if not repo:
                print("skipping %s: maps to no GitOps repository" % name)
                unmapped.append(name)
                continue
            print("sweeping %s (%s)" % (name, repo))
            try:
                closed[name] = sweep_repo(name, repo, app_id, dry_run=dry_run, runner=runner)
            except (
                SweepError,
                urllib.error.HTTPError,
                OSError,
                http.client.HTTPException,
                subprocess.SubprocessError,
            ) as exc:
                print("  %s: %s" % (name, exc), file=sys.stderr)
                failures[name] = str(exc)
        finally:
            boskos_release(server, owner, name)
    print(
        "swept %d project(s): closed %d pull request(s), %d failed, %d unmapped"
        % (len(closed) + len(failures), sum(closed.values()), len(failures), len(unmapped))
    )
    return closed, failures, unmapped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--app-id", default=DEFAULT_APP_ID, help="the agent's GitHub App id (EVAL_GITHUB_APP_ID)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would close, change nothing"
    )
    parser.add_argument(
        "--ci-deploy-script",
        default=str(CI_DEPLOY_SCRIPT),
        help="where gitops_repo_for_project() lives (default: beside this script)",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--pool", action="store_true", help="sweep every project Boskos reports free"
    )
    mode.add_argument("--project", help="sweep one project, without asking Boskos")
    parser.add_argument(
        "--repo", help="with --project: owner/name to sweep instead of the mapped repository"
    )
    parser.add_argument(
        "--boskos-server",
        default=os.environ.get("BOSKOS_SERVER", BOSKOS_DEFAULT_SERVER),
        help="Boskos endpoint (default: $BOSKOS_SERVER, else the in-cluster service)",
    )
    parser.add_argument(
        "--boskos-owner",
        default=os.environ.get("BOSKOS_OWNER") or DEFAULT_BOSKOS_OWNER,
        help="owner name the acquisitions are recorded under",
    )
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _terminate)
    try:
        mapping = pool_repos(args.ci_deploy_script)
        if args.project:
            repo = args.repo or mapping.get(args.project)
            if not repo:
                raise SweepError("%s maps to no GitOps repository" % args.project)
            sweep_repo(args.project, repo, args.app_id, dry_run=args.dry_run, runner=subprocess.run)
            return 0
        _, failures, _ = sweep_pool(
            args.boskos_server,
            args.boskos_owner,
            args.app_id,
            mapping,
            dry_run=args.dry_run,
            runner=subprocess.run,
        )
        if failures:
            print(
                "ERROR: %d project(s) not fully swept: %s"
                % (len(failures), ", ".join(sorted(failures))),
                file=sys.stderr,
            )
            return 1
        return 0
    except Terminated as exc:
        print("ERROR: terminated (%s); held projects were released" % exc, file=sys.stderr)
        return TERMINATED_EXIT_CODE
    except SweepError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        print("ERROR: HTTP %d (%s) from %s" % (exc.code, exc.reason, exc.url), file=sys.stderr)
        return 1
    except (OSError, http.client.HTTPException, subprocess.SubprocessError) as exc:
        print("ERROR: could not reach a service (%s: %s)" % (type(exc).__name__, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
