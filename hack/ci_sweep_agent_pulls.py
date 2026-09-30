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
code. That placement is the point: closing a pull request and deleting its
branch need `pull_requests: write` and `contents: write` on every pool
repository, and a presubmit runs the pull request's own scripts, so a write
credential mounted there is reachable by any change under test. Here the
credential is the agent's own GitHub App, signed through each project's KMS
key (the same key minty signs with in-cluster), by a service account only this
job runs as. The token is narrowed at mint to one repository and those two
writes; the key never leaves KMS.

Which projects: the ones Boskos hands out as `free`. Each is acquired into a
`cleaning` state for the seconds the sweep takes and released back to `free`,
so a project a run holds is never touched, and a run arriving mid-sweep waits
those seconds at its own acquire. No listing endpoint is needed and no run's
state is read.

The writes are paced to GitHub's published burst limits for an App -- a second
between writes, 500 writes an hour -- with a budget of writes per run past which
the rest waits for the next run, logged and reported; a write GitHub refuses is
retried once after the Retry-After it asks for, and refused again the run ends
rather than visiting every project during the cooldown. Every run writes a
report beside the job's artifacts (write_report), which the CI health bot reads.

Three conditions, all required, matching is_agent_pull_request in
agents/platform/scripts/forge.py: authored by the agent's bot, head branch
carrying the agent's prefix, and that branch in the repository itself rather
than a fork. The head branch goes with the pull request: submit_suggestion.py
starts from the remote branch when it exists and refuses "nothing to commit"
when the new tree matches it, so a closed pull request whose branch stayed
would still cost the next lease a repetition (#1755 item 2). Branches under
the prefix with no open pull request are deleted too, so a delete that failed
or a run killed between a close and its delete is caught up by the next run.
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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import boskos_pool  # noqa: E402

API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "kube-agents-pull-sweep"
REQUEST_TIMEOUT_SECONDS = 30
# GitHub's secondary limits for one App: at least a second between writes, and
# no more than 500 content-generating requests an hour, answered otherwise with
# a 403 for every call for some minutes. A close and its branch delete are two
# writes; six runs an hour at this budget stay under 500, and a backlog drains
# across runs instead of in one burst. What a run leaves is logged and reported.
WRITE_PAUSE_SECONDS = 1.0
WRITE_BUDGET_PER_RUN = 80
RATE_LIMITED_CODE = 403
RETRY_AFTER_HEADER = "Retry-After"
# A 403 is the burst limit only when GitHub says so: a Retry-After, a spent
# rate-limit budget in the headers, or a body naming a limit. Any other 403 (an
# archived repository, a protected branch) is that repository's fault, as before.
RATELIMIT_REMAINING_HEADER = "X-RateLimit-Remaining"
RATE_LIMIT_BODY_MARKERS = ("rate limit", "abuse detection")
# Without the header, or with one past this, a refused write waits this long
# once; refused again, the run ends rather than visiting every project during
# the cooldown.
RETRY_AFTER_DEFAULT_SECONDS = 60
RETRY_AFTER_MAX_SECONDS = 120
# The run's report, written last like the reconcile's (hack/fleet_reconcile.py),
# under Prow's artifacts when it sets ARTIFACTS: what the CI health bot names.
REPORT_FILE = "pull-sweep.json"
REPORT_SCHEMA_VERSION = 1
ARTIFACTS_ENV = "ARTIFACTS"
ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
EXIT_NAME_OK = "ok"
EXIT_NAME_FAILED = "failed"
EXIT_NAME_TERMINATED = "terminated"
EXIT_NAME_ERROR = "error"
MODE_POOL = "pool"
MODE_PROJECT = "project"
# The pause between writes, a module attribute so a test can stand in for it.
pause = time.sleep

# Must equal AGENT_BRANCH_PREFIX in agents/platform/scripts/forge.py, which is
# what names the branches these pull requests come from. A test pins the two.
AGENT_BRANCH_PREFIX = "platform-agent/"

# GitHub rejects an App JWT whose exp is more than ten minutes out; nine leaves
# room for clock skew, and the backdated iat covers a slow runner.
JWT_LIFETIME_SECONDS = 540
JWT_BACKDATE_SECONDS = 60

# The App this signs as is the one the agent submits with (hack/ci-deploy.sh
# hands it over as EVAL_GITHUB_APP_ID; a test pins the id to the provisioning
# script), so the author to close for is its own bot: the slug GET /app
# returns under the same JWT, plus this suffix. Read rather than written out,
# so a renamed App is followed instead of silently matching nothing.
BOT_LOGIN_SUFFIX = "[bot]"
DEFAULT_APP_ID = "4675512"

# What this asks for: pull_requests to close, contents to delete the head
# branch (a ref delete is a contents write). The installation carries issues
# write as well, for the agent; a token that inherited it whole would hold
# that here for nothing.
TOKEN_PERMISSIONS = {"pull_requests": "write", "contents": "write"}
# GitHub's answers for a ref that is already gone: 422 "Reference does not
# exist", or 404. Neither is a failure -- the branch is what was wanted absent.
REF_GONE_CODES = (404, 422)

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

# The Boskos walk itself is hack/boskos_pool.py, shared with the fleet
# reconcile. A project is held in BOSKOS_SWEEP_STATE only while its repository
# is being swept.
BOSKOS_DEFAULT_SERVER = boskos_pool.DEFAULT_SERVER
BOSKOS_RESOURCE_TYPE = boskos_pool.RESOURCE_TYPE
BOSKOS_SWEEP_STATE = "cleaning"
BOSKOS_MAX_CONSECUTIVE_REPEATS = boskos_pool.MAX_CONSECUTIVE_REPEATS
DEFAULT_BOSKOS_OWNER = "ci-kube-agents-pull-sweep"
# A sweep killed mid-hold (deadline, node loss) would leave its project in
# BOSKOS_SWEEP_STATE: not free, not busy, unusable. Each run starts by asking
# Boskos to return anything that has sat there longer than this to free -- its
# own /reset, a Go duration. Under the periodic's ten-minute interval, so the
# very next run returns a strand (15m would have let one sit for two or three
# runs). A sweep holds a project for seconds, so nothing live is inside the
# window.
BOSKOS_STRANDED_AFTER = "5m"
TERMINATED_EXIT_CODE = boskos_pool.TERMINATED_EXIT_CODE
Terminated = boskos_pool.Terminated
_terminate = boskos_pool.terminate

# The project-to-repository mapping keeps its one home in hack/ci-deploy.sh; a
# dozen documents and scripts read it out of that file. The lines are
# `    <project>) echo "<owner>/<repo>" ;;` inside gitops_repo_for_project().
CI_DEPLOY_SCRIPT = pathlib.Path(__file__).resolve().parent / "ci-deploy.sh"
MAPPING_FUNCTION = "gitops_repo_for_project"
MAPPING_LINE_RE = re.compile(r'^\s+([A-Za-z0-9-]+)\)\s+echo "([^"/]+/[^"]+)"\s+;;\s*$')


class RateLimited(Exception):
    """GitHub refused a write twice under its burst limit: the run ends here."""


class WriteBudget:
    """The run's remaining writes. A close or a delete takes one; when none is
    left the rest waits for the next run, counted in `left` as the writes it
    will need (a pull request left unclosed is two: its close and its delete)."""

    def __init__(self, writes=None):
        self.budget = WRITE_BUDGET_PER_RUN if writes is None else writes
        self.remaining = self.budget
        self.left = 0
        self.exhausted_at = None

    def take(self, repo, writes_left_if_not=1):
        if self.remaining <= 0:
            if self.exhausted_at is None:
                self.exhausted_at = repo
                print("  write budget for this run (%d) used up at %s; the rest waits for the next run" % (self.budget, repo), file=sys.stderr)
            self.left += writes_left_if_not
            return False
        self.remaining -= 1
        return True


class SweepError(Exception):
    """A fault that stops one repository's sweep. The caller reports it."""


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
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        boskos_pool.error_body(exc)
        raise
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        # A 200 whose body is not JSON (an intermediary's page mid-incident):
        # one repository's fault, reported as such rather than as a traceback.
        raise SweepError("GitHub answered %s %s with a body that is not JSON: %s" % (method, path, exc))


def open_pulls(repo, authorization):
    """Every open pull request in the repository, by page."""
    found = []
    for page in range(1, MAX_PAGES + 1):
        batch = api(
            "GET",
            "/repos/%s/pulls?state=open&per_page=%d&page=%d" % (repo, PER_PAGE, page),
            authorization,
        )
        if not isinstance(batch, list) or not all(isinstance(pull, dict) for pull in batch):
            # A 200 that is not a list of pull requests (an object, a null, an
            # empty body): a fault to report, never "closed 0" over a
            # repository that was not read. GitHub's last page is `[]`.
            raise SweepError("GitHub answered the pull-request listing for %s with a body that is not a list of pull requests" % repo)
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
    """(token, bot login): a token for this repository alone, and who the App is.

    The installation is resolved from the repository rather than passed in: one
    App serves the whole pool, and a hardcoded id is a silent 404 on every
    repository but one. The token is narrowed twice -- to this one repository,
    and to TOKEN_PERMISSIONS -- so the sweep never holds the reach the App has.
    The author to close for comes from the same credential: GET /app under the
    JWT names the App's slug, and its pull requests are authored by that plus
    BOT_LOGIN_SUFFIX.
    """
    bearer = "Bearer " + app_jwt(app_id, project, runner)
    try:
        slug = _field(api("GET", "/app", bearer), "slug", "the App lookup", repo)
        installation = _field(api("GET", "/repos/%s/installation" % repo, bearer), "id", "the installation lookup", repo)
    except urllib.error.HTTPError as exc:
        # 401: the key in this project's KMS is not App app_id's. 404: the App
        # is not installed on this repository, an onboarding gap rather than a
        # fault here.
        raise SweepError(
            "GitHub answered HTTP %d (%s) locating App %s and its installation on %s"
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
    return _field(minted, "token", "the token mint", repo), slug + BOT_LOGIN_SUFFIX


def _field(payload, key, what, repo):
    """`payload[key]` from a JSON object, or a SweepError naming the shapeless answer."""
    if not isinstance(payload, dict) or key not in payload or payload[key] in (None, ""):
        raise SweepError("GitHub answered %s for %s without a %r field" % (what, repo, key))
    return payload[key]


CALL_FAULTS = (urllib.error.HTTPError, OSError, http.client.HTTPException, SweepError)


def _retry_after(exc):
    """Seconds GitHub asked for, bounded; the default when it named none."""
    raw = (getattr(exc, "headers", None) or {}).get(RETRY_AFTER_HEADER)
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return RETRY_AFTER_DEFAULT_SECONDS
    return max(0, min(seconds, RETRY_AFTER_MAX_SECONDS))


def is_rate_limited(exc):
    """A 403 that GitHub marks as its burst limit, not a permission."""
    if exc.code != RATE_LIMITED_CODE:
        return False
    headers = getattr(exc, "headers", None) or {}
    if headers.get(RETRY_AFTER_HEADER) is not None or str(headers.get(RATELIMIT_REMAINING_HEADER, "")).strip() == "0":
        return True
    body = boskos_pool.error_body(exc).lower()
    return any(marker in body for marker in RATE_LIMIT_BODY_MARKERS)


def write(method, path, authorization, body=None):
    """One GitHub write, paced. A 403 GitHub marks as its burst limit is waited
    out once, for what it asks, and tried again; refused again, the run ends
    here rather than visiting every project during the cooldown. Any other
    error is the caller's, as before."""
    try:
        result = api(method, path, authorization, body)
    except urllib.error.HTTPError as exc:
        if not is_rate_limited(exc):
            raise
        wait = _retry_after(exc)
        print("  %s %s refused (%s); waiting %ds before one retry" % (method, path, boskos_pool.describe(exc), wait), file=sys.stderr)
        pause(wait)
        try:
            result = api(method, path, authorization, body)
        except urllib.error.HTTPError as again:
            if not is_rate_limited(again):
                raise
            raise RateLimited("GitHub refused %s %s twice (%s)" % (method, path, boskos_pool.describe(again)))
    pause(WRITE_PAUSE_SECONDS)
    return result


def delete_branch(repo, ref, authorization):
    """Delete `ref` (a branch name) from the repository. Already gone is fine."""
    try:
        write("DELETE", "/repos/%s/git/refs/heads/%s" % (repo, urllib.parse.quote(ref, safe="/")), authorization)
    except urllib.error.HTTPError as exc:
        if exc.code not in REF_GONE_CODES:
            raise


def agent_branches(repo, authorization):
    """Every branch under AGENT_BRANCH_PREFIX in the repository itself."""
    refs = api("GET", "/repos/%s/git/matching-refs/heads/%s" % (repo, AGENT_BRANCH_PREFIX), authorization)
    if not isinstance(refs, list) or not all(isinstance(ref, dict) for ref in refs):
        raise SweepError("GitHub answered the branch listing for %s with a body that is not a list of refs" % repo)
    names = [str(ref.get("ref") or "") for ref in refs]
    return [name[len("refs/heads/"):] for name in names if name.startswith("refs/heads/" + AGENT_BRANCH_PREFIX)]


def close_agent_pulls(repo, authorization, bot_login, dry_run=False, budget=None):
    """Close every open pull request `bot_login` owns, and delete its branch --
    and any branch under the agent's prefix an earlier run left behind.

    Returns (closed, deleted, unclosed, undeleted): the counts, the numbers
    that would not close, and the branches that would not delete. With a
    `budget` (the pool walk's), each write takes one from it and a write it
    cannot pay for is left for the next run; without one (a hand run of one
    project) every write is made, paced. A RateLimited from a write ends the
    sweep of this repository and is the caller's to end the run on.
    """
    closed = 0
    deleted = 0
    unclosed = []
    undeleted = []
    pulls = open_pulls(repo, authorization)
    still_open = set()
    gone = set()
    deferred = set()
    for pull in pulls:
        if not is_agent_pull_request(pull, repo, bot_login):
            still_open.add(str((pull.get("head") or {}).get("ref") or ""))
            continue
        number = pull["number"]
        ref = pull["head"]["ref"]
        print("  #%s (%s)" % (number, ref))
        if dry_run:
            closed += 1
            gone.add(ref)
            continue
        if budget is not None and not budget.take(repo, writes_left_if_not=2):
            still_open.add(ref)
            continue
        # Each close stands alone. One that fails is reported and the sweep
        # carries on: giving up here would leave every later pull request open,
        # which is the thing being fixed. HTTPException covers a response cut
        # short mid-read, which urllib does not raise as OSError.
        try:
            write(
                "PATCH",
                "/repos/%s/pulls/%s" % (repo, number),
                authorization,
                {"state": "closed"},
            )
        except CALL_FAULTS as exc:
            print("  #%s did not close (%s)" % (number, boskos_pool.describe(exc)), file=sys.stderr)
            unclosed.append(number)
            still_open.add(ref)
            continue
        closed += 1
        # The branch only after the close: a branch deleted first would leave
        # the pull request open on a head that no longer exists. A branch the
        # budget cannot pay for is a closed pull request's, which the next
        # run's branch pass below deletes.
        if budget is not None and not budget.take(repo):
            deferred.add(ref)
            continue
        try:
            delete_branch(repo, ref, authorization)
            deleted += 1
            gone.add(ref)
        except CALL_FAULTS as exc:
            print("  #%s closed but %s was not deleted (%s)" % (number, ref, boskos_pool.describe(exc)), file=sys.stderr)
            undeleted.append(ref)
    # Branches an earlier run left behind -- a delete that failed, a job killed
    # between a close and its delete -- belong to closed pull requests, which
    # no later listing of open ones finds. So the branches are listed too:
    # every one under the agent's prefix in the repository itself goes, unless
    # an open pull request (anyone's) still has it as head. In the repository
    # itself only the agent pushes under its prefix; the prefix-alone caveat
    # is about forks, which this listing never reaches.
    for ref in agent_branches(repo, authorization):
        if ref in gone or ref in still_open or ref in undeleted or ref in deferred:
            continue
        print("  branch %s (no open pull request)" % ref)
        if dry_run:
            deleted += 1
            continue
        if budget is not None and not budget.take(repo):
            continue
        try:
            delete_branch(repo, ref, authorization)
            deleted += 1
        except CALL_FAULTS as exc:
            print("  %s was not deleted (%s)" % (ref, boskos_pool.describe(exc)), file=sys.stderr)
            undeleted.append(ref)
    return closed, deleted, unclosed, undeleted


def sweep_repo(project, repo, app_id, dry_run=False, runner=subprocess.run, budget=None):
    """Close the agent's leftovers in one project's repository; returns the count."""
    token, bot_login = scoped_token(app_id, project, repo, runner)
    closed, deleted, unclosed, undeleted = close_agent_pulls(repo, "token " + token, bot_login, dry_run=dry_run, budget=budget)
    verb = "would close" if dry_run else "closed"
    print(
        "%s %d pull request(s) by %s and %s %d branch(es) in %s"
        % (verb, closed, bot_login, "would delete" if dry_run else "deleted", deleted, repo)
    )
    faults = []
    if unclosed:
        faults.append("left %d pull request(s) open: %s" % (len(unclosed), ", ".join("#%s" % n for n in unclosed)))
    if undeleted:
        faults.append("left %d branch(es): %s" % (len(undeleted), ", ".join(undeleted)))
    if faults:
        raise SweepError("%s: %s" % (repo, "; ".join(faults)))
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


def boskos_reset_stranded(server):
    """Projects an earlier sweep left in the sweep state, returned to free."""
    return boskos_pool.reset_stranded(server, BOSKOS_SWEEP_STATE, BOSKOS_STRANDED_AFTER, "sweep")


def sweep_pool(server, owner, app_id, mapping, dry_run=False, runner=subprocess.run, report=None):
    """Sweep every project Boskos will hand out as free, once each.

    Returns (closed_by_project, failures_by_project, unmapped). A project is
    released in every path, including a fault mid-sweep; holding one would take
    it out of the pool until a human noticed. `report`, a dict, receives what
    the run left for the next one (`left`) and why it ended early, if it did
    (`ended_early`).
    """
    boskos_reset_stranded(server)
    # The run's record is filled as it goes, in the caller's dict when given,
    # so a report written after a termination or a crash names what was done.
    report = report if report is not None else {}
    closed = report.setdefault("closed", {})
    failures = report.setdefault("failures", {})
    unmapped = report.setdefault("unmapped", [])
    skipped = report.setdefault("skipped", [])
    report.setdefault("left", 0)
    report.setdefault("ended_early", None)
    budget = WriteBudget()

    def visit(name):
        repo = mapping.get(name)
        if not repo:
            print("skipping %s: maps to no GitOps repository" % name)
            unmapped.append(name)
            return
        if report["ended_early"]:
            # GitHub's cooldown covers every repository; the project is held
            # and released so the walk still ends, and nothing is asked of it.
            print("skipping %s: %s" % (name, report["ended_early"]))
            skipped.append(name)
            return
        print("sweeping %s (%s)" % (name, repo))
        try:
            closed[name] = sweep_repo(name, repo, app_id, dry_run=dry_run, runner=runner, budget=budget)
        except RateLimited as exc:
            print("  %s: %s" % (name, exc), file=sys.stderr)
            failures[name] = str(exc)
            report["ended_early"] = str(exc)
        except (
            SweepError,
            urllib.error.HTTPError,
            OSError,
            http.client.HTTPException,
            subprocess.SubprocessError,
        ) as exc:
            print("  %s: %s" % (name, boskos_pool.describe(exc)), file=sys.stderr)
            failures[name] = boskos_pool.describe(exc)
        finally:
            report["left"] = budget.left

    # The hold is heartbeated: one repository's sweep can now run for minutes
    # (paced writes, a Retry-After wait), and the pool's reaper frees a hold
    # not updated for about five minutes.
    _, release_failures = boskos_pool.walk(server, owner, BOSKOS_SWEEP_STATE, len(mapping), visit, heartbeat=True)
    failures.update(release_failures)
    print(
        "swept %d project(s): closed %d pull request(s), %d failed, %d unmapped, %d skipped, %d write(s) left for the next run"
        % (len(closed) + len(failures), sum(closed.values()), len(failures), len(unmapped), len(skipped), budget.left)
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
    parser.add_argument(
        "--report",
        default=default_report_path(),
        help="where to write the run's %s (default: $%s/%s when Prow sets it, else nowhere)" % (REPORT_FILE, ARTIFACTS_ENV, REPORT_FILE),
    )
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _terminate)
    started = time.time()
    run = {"closed": {}, "failures": {}, "unmapped": [], "skipped": [], "left": 0, "ended_early": None}
    code = None
    error = None
    try:
        code, error = _run(args, run)
        return code
    except Terminated as exc:
        error = "terminated (%s); held projects were released" % exc
        print("ERROR: %s" % error, file=sys.stderr)
        code = TERMINATED_EXIT_CODE
        return code
    except BaseException as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
        raise
    finally:
        if args.report:
            write_report(args.report, args, run, code, error, started)


def _run(args, run):
    """The sweep itself; (exit code, error line or None)."""
    try:
        mapping = pool_repos(args.ci_deploy_script)
        if args.project:
            repo = args.repo or mapping.get(args.project)
            if not repo:
                raise SweepError("%s maps to no GitOps repository" % args.project)
            # A hand run: paced, but every write is made; there is no next run.
            try:
                run["closed"][args.project] = sweep_repo(args.project, repo, args.app_id, dry_run=args.dry_run, runner=subprocess.run)
            except RateLimited as exc:
                run["failures"][args.project] = str(exc)
                run["ended_early"] = str(exc)
                print("ERROR: %s" % exc, file=sys.stderr)
                return 1, str(exc)
            return 0, None
        _, failures, _ = sweep_pool(
            args.boskos_server,
            args.boskos_owner,
            args.app_id,
            mapping,
            dry_run=args.dry_run,
            runner=subprocess.run,
            report=run,
        )
        if failures:
            error = "%d project(s) not fully swept: %s" % (len(failures), ", ".join(sorted(failures)))
            print("ERROR: %s" % error, file=sys.stderr)
            return 1, error
        return 0, None
    except (SweepError, boskos_pool.BoskosError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1, str(exc)
    except urllib.error.HTTPError as exc:
        error = "HTTP %d (%s) from %s: %s" % (exc.code, exc.reason, exc.url, boskos_pool.error_body(exc))
        print("ERROR: %s" % error, file=sys.stderr)
        return 1, error
    except (OSError, http.client.HTTPException, subprocess.SubprocessError) as exc:
        error = "could not reach a service (%s: %s)" % (type(exc).__name__, exc)
        print("ERROR: %s" % error, file=sys.stderr)
        return 1, error


def default_report_path():
    artifacts = os.environ.get(ARTIFACTS_ENV)
    return str(pathlib.Path(artifacts) / REPORT_FILE) if artifacts else None


def write_report(path, args, run, code, error, started):
    """The run as one JSON document, written last: what the CI health bot names."""
    names = {0: EXIT_NAME_OK, 1: EXIT_NAME_FAILED, TERMINATED_EXIT_CODE: EXIT_NAME_TERMINATED}
    outcomes = {}
    for project, count in sorted(run["closed"].items()):
        outcomes[project] = {"closed": count}
    for project, text in sorted(run["failures"].items()):
        outcomes[project] = {"error": text}
    document = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": MODE_PROJECT if args.project else MODE_POOL,
        "dry_run": bool(args.dry_run),
        "started_at": time.strftime(ISO_UTC_FORMAT, time.gmtime(started)),
        "finished_at": time.strftime(ISO_UTC_FORMAT, time.gmtime(time.time())),
        "exit": names.get(code, EXIT_NAME_ERROR),
        "exit_code": code,
        "error": error,
        "ended_early": run.get("ended_early"),
        "projects": len(outcomes),
        "closed": sum(run["closed"].values()),
        "failed": len(run["failures"]),
        "unmapped": list(run["unmapped"]),
        "skipped": list(run.get("skipped") or []),
        "left_for_next_run": run.get("left", 0),
        "outcomes": outcomes,
    }
    try:
        pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(path).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print("WARNING: could not write the report to %s (%s)" % (path, exc), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
