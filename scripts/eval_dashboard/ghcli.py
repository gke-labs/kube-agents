"""The one way the health job talks to GitHub: `gh api`, best-effort.

The health job's writers (the comment on a red pull request, the tracking
and nightly issues) and the one-off scrub_bot_posts.py write to GitHub with
the token in GH_TOKEN, through the `gh` binary the runner already has -- the same
call coverage-comment.yml makes for its sticky comment, so no new dependency
and no token handling here: `gh` reads GH_TOKEN from the environment and this
module never sees it.

Every call is best-effort. A failure (no binary, no token, a 4xx, a timeout)
is logged -- the status and gh's first line of stderr, never the body -- and
comes back as None; the callers treat None as "not this tick" and the job
carries on. Nothing here raises past a caller.

`--dry-run` turns writes into a printed description of the request; reads
still happen, so a dry run can say "would edit comment 123" rather than
"would post".

Two repositories. SOURCE_REPO is this one: the pull requests, the case
notes' issues and the domains.yaml allowlist issues live there whatever else
is set. DEFAULT_REPO is where the bots file and read their own issues:
SOURCE_REPO unless KUBE_AGENTS_ISSUES_REPO names another (a private triage
repository), in which case the workflow also mints a token that can write
there. The issue filers and their readers follow it through their `--repo`
default: nightly_bugs, coverage_gaps, post_health and gate_issue,
understudies, weekly_email, and nightly_digest through the Gh they are
handed. gate_comment does not: it comments on pull requests, which stay in
SOURCE_REPO. A reference to an issue of SOURCE_REPO is written "#N", one
to any other repository as its full URL (issue_tag), so it resolves wherever
it is read.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

GH_BIN = "gh"
SOURCE_REPO = "gke-labs/kube-agents"
ISSUES_REPO_ENV = "KUBE_AGENTS_ISSUES_REPO"
# What the variable may hold: owner/name, the shape the workflow's split
# step checks too; anything else is a warning and SOURCE_REPO.
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
INVALID_REPO_WARNING = "warning: {env}={value!r} is not owner/name; the issues go to {repo}"
ISSUE_URL = "https://github.com/{repo}/issues/{number}"
# A pull request's page: pull requests stay in SOURCE_REPO, so a body that
# may be filed elsewhere links them in full.
PULL_URL = "https://github.com/{repo}/pull/{number}"
# The two forms an issue reference takes: "#N", an issue of SOURCE_REPO (how
# case-notes.yaml and the domains.yaml allowlist write theirs), or the full
# URL the bots write. issue_key reads either to (repo, number), so a list
# can be searched for an issue whichever form it was written in.
ISSUE_REF_RE = re.compile(r"^(?:#|https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/issues/)(?P<number>\d+)$")
# Where gh reads the token: the workflow's own, or the issues repository's
# minted one once KUBE_AGENTS_ISSUES_REPO names another repository. Then the
# workflow's own rides beside it as SOURCE_GH_TOKEN, for the calls that still
# address SOURCE_REPO: a carried issue filed there before the switch, which
# the minted token (scoped to the issues repository) cannot comment on or
# close.
TOKEN_ENV = "GH_TOKEN"
SOURCE_TOKEN_ENV = "SOURCE_GH_TOKEN"
# One call's ceiling; a hung `gh` must not eat the 30-minute job.
CALL_TIMEOUT_S = 60
# A paginated read comes back one object per line (`--jq '.[]'`), which is
# what stays valid JSON when gh concatenates pages.
PAGINATE_JQ = ".[]"
WRITE_METHODS = ("POST", "PATCH", "DELETE")
DRY_RUN_PREFIX = "--dry-run: would"
# GitHub's answer to a field it will not take (an assignee who is not a
# collaborator): gh prints the status to stderr and the JSON body, which
# names the field, to stdout.
VALIDATION_FAILED = "HTTP 422"


def log(message: str) -> None:
    print(message, file=sys.stderr)


def issues_repo_setting(environ=os.environ) -> str:
    """Where the bots file: the ISSUES_REPO_ENV value when it is owner/name,
    else SOURCE_REPO (a malformed value is logged and treated as unset, as
    the workflow's split step treats it)."""
    value = environ.get(ISSUES_REPO_ENV) or ""
    if not value:
        return SOURCE_REPO
    if not REPO_RE.match(value):
        log(INVALID_REPO_WARNING.format(env=ISSUES_REPO_ENV, value=value, repo=SOURCE_REPO))
        return SOURCE_REPO
    return value


# Read once at import, as every bot's `--repo` default.
DEFAULT_REPO = issues_repo_setting()


def issue_url(repo: str, number) -> str:
    """The issue's page, the one form of an issue reference the bots write."""
    return ISSUE_URL.format(repo=repo, number=number)


def pr_url(repo: str, number) -> str:
    """The pull request's page."""
    return PULL_URL.format(repo=repo, number=number)


def token_environ(repo: str, environ=os.environ) -> dict | None:
    """The environment a `gh` call to ``repo`` runs with: the caller's with
    GH_TOKEN swapped for SOURCE_GH_TOKEN when the call addresses SOURCE_REPO
    while the bots file elsewhere; None when the caller's own will do (one
    repository, or no SOURCE_GH_TOKEN to swap in)."""
    if repo != SOURCE_REPO or DEFAULT_REPO == SOURCE_REPO or not environ.get(SOURCE_TOKEN_ENV):
        return None
    return {**environ, TOKEN_ENV: environ[SOURCE_TOKEN_ENV]}


def issue_key(ref) -> tuple[str, int] | None:
    """``(repo, number)`` for "#N" (SOURCE_REPO) or an issue URL; None for
    anything else."""
    match = ISSUE_REF_RE.match(ref.strip()) if isinstance(ref, str) else None
    if not match:
        return None
    return (match["repo"] or SOURCE_REPO, int(match["number"]))


def has_issue(refs, ref) -> bool:
    """Whether ``refs`` names the issue ``ref`` does, in either form."""
    key = issue_key(ref)
    return key is not None and any(issue_key(each) == key for each in refs or [])


def issue_repo(issue) -> str:
    """The repository an {number, url} issue lives in, read from its `url`;
    SOURCE_REPO for a record without a GitHub issue URL (one written before
    the issues repository existed)."""
    key = issue_key((issue or {}).get("url")) if isinstance(issue, dict) else None
    return key[0] if key else SOURCE_REPO


def issue_tag(issue) -> str | None:
    """How the bots write a reference to the {number, url} issue they filed
    or adopted: "#N" for an issue of SOURCE_REPO (unchanged while no issues
    repository is set), its full URL for one elsewhere, so the reference
    resolves wherever it is read; None for anything that is not one."""
    if not isinstance(issue, dict) or not issue.get("number"):
        return None
    repo = issue_repo(issue)
    return f"#{issue['number']}" if repo == SOURCE_REPO else issue_url(repo, issue["number"])


class Gh:
    """`gh api` with an injectable runner (tests pass a recording fake)."""

    def __init__(self, repo: str = DEFAULT_REPO, runner=subprocess.run, dry_run: bool = False, binary: str = GH_BIN):
        self.repo = repo
        self.runner = runner
        self.dry_run = dry_run
        self.binary = binary
        # What the last failed call printed (gh's stderr and stdout), for
        # refused(); None after a call that went through. Never logged.
        self.last_failure: str | None = None

    def refused(self, field: str) -> bool:
        """Whether the last call failed because GitHub refused ``field``
        (a 422 naming it), as opposed to a timeout, a 5xx or a 403."""
        text = self.last_failure or ""
        return VALIDATION_FAILED in text and field in text

    def path(self, suffix: str) -> str:
        return f"repos/{self.repo}/{suffix}"

    def call(self, method: str, path: str, body: dict | None = None, paginate: bool = False, read: bool = False):
        """The decoded JSON response, a list for a paginated read, or None.
        ``read`` marks a POST that only reads (a GraphQL query), which a dry
        run still sends."""
        self.last_failure = None
        if self.dry_run and method in WRITE_METHODS and not read:
            log(f"{DRY_RUN_PREFIX} {method} {path}\n{json.dumps(body, indent=2, ensure_ascii=False) if body else ''}")
            return None
        argv = [self.binary, "api", "-X", method, path]
        if paginate:
            argv += ["--paginate", "--jq", PAGINATE_JQ]
        if body is not None:
            argv += ["--input", "-"]
        env = token_environ(self.repo)
        try:
            proc = self.runner(
                argv,
                input=json.dumps(body) if body is not None else None,
                capture_output=True,
                text=True,
                timeout=CALL_TIMEOUT_S,
                check=False,
                **({"env": env} if env is not None else {}),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.last_failure = type(exc).__name__
            log(f"warning: gh api {method} {path}: {type(exc).__name__}; skipped")
            return None
        if proc.returncode != 0:
            first = (proc.stderr or "").strip().splitlines()
            self.last_failure = f"{proc.stderr or ''}\n{proc.stdout or ''}"
            log(f"warning: gh api {method} {path} failed (exit {proc.returncode}): {first[0] if first else 'no stderr'}")
            return None
        text = proc.stdout or ""
        try:
            if paginate:
                return [json.loads(line) for line in text.splitlines() if line.strip()]
            return json.loads(text) if text.strip() else {}
        except ValueError:
            log(f"warning: gh api {method} {path}: unparseable response; skipped")
            return None
