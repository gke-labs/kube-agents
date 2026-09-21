#!/usr/bin/env python3
"""Close the platform agent's leftover pull requests in a leased project's GitOps repo.

A remediation scenario opens a pull request there, and nothing closed it. The
next lease of that project meets its predecessor's: `create_pull_request` in
agents/platform/scripts/submit_suggestion.py treats "a pull request already
exists" as success and returns the old one's URL, so a repetition that opened
nothing could be graded as if it had (#1755).

Called from hack/ci-teardown.sh, which the Prow wrapper also runs at job start —
so a lease begins clean even when the run before it was hard-killed.

Three conditions, all required, matching is_agent_pull_request in
agents/platform/scripts/forge.py: authored by this App's own bot login, head
branch carrying the agent's prefix, and that branch in the repository itself
rather than a fork. Branches are left alone: deleting a ref needs
`contents: write`, which this credential deliberately does not have, and the
agent pushes with --force-with-lease from a fresh clone, so a stale branch
costs nothing.
"""

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "kube-agents-ci-teardown"
REQUEST_TIMEOUT_SECONDS = 30

# Must equal AGENT_BRANCH_PREFIX in agents/platform/scripts/forge.py, which is
# what names the branches these pull requests come from. A test pins the two.
AGENT_BRANCH_PREFIX = "platform-agent/"

# GitHub rejects an App JWT whose exp is more than ten minutes out; nine leaves
# room for clock skew, and the backdated iat covers a slow runner.
JWT_LIFETIME_SECONDS = 540
JWT_BACKDATE_SECONDS = 60
OPENSSL_ERROR_CHARS = 300

# An App acts through a bot account named for its slug, and that login authors
# the pull requests an installation token opens.
BOT_LOGIN_SUFFIX = "[bot]"

# The only permission this asks for, and the only one it needs. The App can do
# more -- it also reads issues, for the ledger checks -- so the token is
# narrowed at mint time rather than inherited whole.
TOKEN_PERMISSIONS = {"pull_requests": "write"}

PER_PAGE = 100
# A bound rather than a budget: orders of magnitude above any pool repository,
# so a paging bug cannot spin here for the teardown's whole window.
MAX_PAGES = 20

# What GitHub answers when the installation has not accepted a permission the
# mint asked for. Its message is about the token, which reads as a code fault;
# the cause is a pending click in the organisation's settings.
PERMISSION_NOT_GRANTED_CODES = (403, 422)


class SweepError(Exception):
    """A fault that stops the sweep. The caller prints it and exits nonzero."""


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def app_jwt(app_id, key_file):
    """Sign an App JWT with openssl.

    openssl rather than a JWT library: the Prow image has no PyJWT, and
    hack/ci-eval-pr.sh already mints this App's read token the same way.
    """
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
    signed = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", key_file],
        input=signing_input,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if signed.returncode != 0:
        raise SweepError(
            "openssl could not sign with %s: %s"
            % (key_file, signed.stderr.decode()[:OPENSSL_ERROR_CHARS])
        )
    return (signing_input + b"." + _b64(signed.stdout)).decode("ascii")


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


def scoped_token(app_id, key_file, repo):
    """A token for this repository alone, and the App's bot login.

    The installation is resolved from the repository rather than passed in: one
    App serves thirty of them, and a hardcoded id is a silent 404 on the other
    twenty-nine. The token is narrowed twice -- to this one repository, and to
    TOKEN_PERMISSIONS -- so a teardown never holds the reach the App has.
    """
    bearer = "Bearer " + app_jwt(app_id, key_file)
    try:
        slug = api("GET", "/app", bearer)["slug"]
        installation = api("GET", "/repos/%s/installation" % repo, bearer)["id"]
        minted = api(
            "POST",
            "/app/installations/%s/access_tokens" % installation,
            bearer,
            {"repositories": [repo.split("/")[-1]], "permissions": dict(TOKEN_PERMISSIONS)},
        )
    except urllib.error.HTTPError as exc:
        if exc.code in PERMISSION_NOT_GRANTED_CODES:
            raise SweepError(
                "App %s cannot mint %s on %s (HTTP %d). An organisation owner has to accept "
                "the permission on the App's installation before this sweep can run."
                % (app_id, sorted(TOKEN_PERMISSIONS), repo, exc.code)
            )
        # 401: the key is not App app_id's. 404: the App is not installed on
        # this repository, which is an onboarding gap rather than a fault here.
        raise SweepError(
            "GitHub answered HTTP %d (%s) minting for App %s on %s"
            % (exc.code, exc.reason, app_id, repo)
        )
    return minted["token"], slug + BOT_LOGIN_SUFFIX


def sweep(repo, app_id, key_file, dry_run=False):
    """Close every pull request in the repository this agent owns."""
    token, bot_login = scoped_token(app_id, key_file, repo)
    authorization = "token " + token

    closed = 0
    for pull in open_pulls(repo, authorization):
        if not is_agent_pull_request(pull, repo, bot_login):
            continue
        number = pull["number"]
        print("  #%s (%s)" % (number, pull["head"]["ref"]))
        if not dry_run:
            api(
                "PATCH",
                "/repos/%s/pulls/%s" % (repo, number),
                authorization,
                {"state": "closed"},
            )
        closed += 1

    print(
        "%s %d pull request(s) by %s in %s"
        % ("would close" if dry_run else "closed", closed, bot_login, repo)
    )
    return closed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name of the GitOps repository")
    parser.add_argument("--app-id", required=True, help="GitHub App id to mint as")
    parser.add_argument("--key-file", required=True, help="path to the App's PEM")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would close, change nothing"
    )
    args = parser.parse_args(argv)
    try:
        sweep(args.repo, args.app_id, args.key_file, dry_run=args.dry_run)
    except SweepError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        print(
            "ERROR: GitHub answered HTTP %d (%s) sweeping %s" % (exc.code, exc.reason, args.repo),
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(
            "ERROR: could not reach api.github.com to sweep %s (%s: %s)"
            % (args.repo, type(exc).__name__, exc),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
