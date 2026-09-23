"""Close the open audit ledger issues in ONE leased project's GitOps repository.

A fleet-audit stream keeps one open issue per audit in the project's GitOps
repository (`gke-agentic/<project>-infra`): `audit_report.py start` finds it as
the highest open issue labelled `audit:<id>` and, since #1691, hands the worker
every finding its body carries by name; `finish` rewrites it. Nothing closes
it between eval runs, so on a pool project every repetition of an audit case
starts with the ledger the previous lease -- and, inside one run, the previous
repetition -- left behind, carrying the planted defect already. A repetition
can then pass by keeping a carried finding rather than finding it, and a false
clean close by repetition 2 leaves repetition 3 a different start than 1.

This closes those issues so the next `start` finds none and opens a fresh
ledger. hack/ci-eval-pr.sh calls it twice: once when the lease begins, for
every audit stream, and once per repetition of an audit case, for that stream
alone, under the case's task lock. Every repetition therefore audits from an
empty ledger; the price is one closed issue per repetition in a repository
that exists to be written to.

What it will not do:

* touch any repository but the leased project's. The caller names both, and
  `expected_repo` refuses a pair that does not match; the token the caller
  minted is narrowed to that one repository as well, so the two guards fail
  independently.
* close an issue that is not a ledger. Three conditions, all required: the
  `agent:audit` label (and `audit:<id>` when one stream is named), the
  `[audit] ` title prefix `render_issue_title` writes, and a `[bot]` author.
  A human's issue in the repository, labelled or not, stays open.
* delete anything. Closing is a state change with a comment naming the eval
  build; the issue and its history stay readable. The comment opens with a
  fixed marker (`RESET_MARKER`) that the `ledger_issue_contains` check reads
  back, so a report that still cites a retired ledger is graded as a stale
  pointer to the harness's close, not as a run that closed its own ledger.

The token arrives in the environment (`LEDGER_RESET_TOKEN`), never on argv
where `ps` would show it. `--dry-run` lists what would close and writes
nothing, which is how this is checked from a laptop with a personal token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "kube-agents-ci-eval-pr"
REQUEST_TIMEOUT_SECONDS = 30
PER_PAGE = 100
MAX_PAGES = 10

TOKEN_ENV = "LEDGER_RESET_TOKEN"
# The label every ledger carries and the per-stream one beside it, as
# audit_report.py writes them (`--label agent:audit --label audit:<id>`).
AUDIT_LABEL = "agent:audit"
STREAM_LABEL_PREFIX = "audit:"
# render_issue_title's prefix. A labelled issue without it is not a ledger.
TITLE_PREFIX = "[audit] "
BOT_LOGIN_SUFFIX = "[bot]"
# The one organisation that hosts every pool repository, and the suffix the
# mapping in hack/ci-deploy.sh gives each: gitops_repo_for_project() is the
# mapping's home, this is the shape check on what the caller resolved there.
REPO_OWNER = "gke-agentic"
REPO_SUFFIX = "-infra"
CLOSE_REASON = "not_planned"
# The first line of every closing comment. bench/kube_agents_bench/verifiers.py
# (LEDGER_RESET_MARKER) looks for it on a closed ledger a report still cites,
# so the grader says "the harness retired this before the run" rather than
# blaming the run for a close it did not make. scripts/test_ci_eval_ledger_reset.py
# pins the two literals equal.
RESET_MARKER = "<!-- kube-agents-eval-ledger-reset -->"


class ResetError(Exception):
    """A fault that stops the reset. The caller prints it and exits nonzero."""


def expected_repo(repo: str, project: str) -> None:
    """Refuse unless `repo` is the leased project's own repository.

    The mapping lives in hack/ci-deploy.sh; this checks the result against
    the project rather than trusting the caller, because the one failure
    worth refusing outright is a close in some other project's repository.
    """
    if not project:
        raise ResetError("no PROJECT_ID: refusing to reset ledgers in an unnamed lease")
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        raise ResetError(f"{repo!r} is not an owner/name repository")
    if owner != REPO_OWNER:
        raise ResetError(
            f"{repo} is not in the {REPO_OWNER} organisation, where every pool repository "
            "lives; refusing to touch it"
        )
    if name != project + REPO_SUFFIX:
        raise ResetError(
            f"{repo} is not the GitOps repository of the leased project {project} "
            f"(expected {REPO_OWNER}/{project}{REPO_SUFFIX}); refusing to touch it"
        )


def api(method: str, path: str, token: str, body: dict | None = None):
    """One GitHub call. Returns the decoded body, or None when it is empty."""
    data = None
    headers = {
        "Authorization": "Bearer " + token,
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
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        # A 200 whose body is not JSON (a maintenance page, say) is the same
        # to the caller as an API it could not reach: reported, never a traceback.
        raise OSError(f"GitHub answered {method} {path} with a body that is not JSON: {exc}") from exc


def label_names(issue: dict) -> set[str]:
    names = set()
    for label in issue.get("labels") or []:
        if isinstance(label, dict):
            names.add(str(label.get("name") or ""))
        else:
            names.add(str(label))
    return names


def is_ledger(issue: dict, audit_id: str | None) -> bool:
    """The three conditions above, over a REST issue object.

    `/issues` also lists pull requests; one is never a ledger.
    """
    if issue.get("pull_request"):
        return False
    labels = label_names(issue)
    if AUDIT_LABEL not in labels:
        return False
    if audit_id:
        if STREAM_LABEL_PREFIX + audit_id not in labels:
            return False
    elif not any(name.startswith(STREAM_LABEL_PREFIX) for name in labels):
        return False
    if not str(issue.get("title") or "").startswith(TITLE_PREFIX):
        return False
    author = str((issue.get("user") or {}).get("login") or "")
    return author.endswith(BOT_LOGIN_SUFFIX)


def open_ledgers(repo: str, token: str, audit_id: str | None) -> list[dict]:
    """Every open ledger issue in the repository, oldest first."""
    labels = AUDIT_LABEL if not audit_id else f"{AUDIT_LABEL},{STREAM_LABEL_PREFIX}{audit_id}"
    query = {"state": "open", "labels": labels, "per_page": str(PER_PAGE)}
    found = []
    for page in range(1, MAX_PAGES + 1):
        query["page"] = str(page)
        batch = api("GET", f"/repos/{repo}/issues?" + urllib.parse.urlencode(query), token)
        if not batch:
            break
        found.extend(issue for issue in batch if is_ledger(issue, audit_id))
        if len(batch) < PER_PAGE:
            break
    found.sort(key=lambda issue: int(issue.get("number") or 0))
    return found


def closing_comment(build: str, scope: str) -> str:
    return (
        f"{RESET_MARKER}\n"
        f"Closed by kube-agents eval build {build} {scope}: the eval harness's ledger "
        "reset retired it so that every repetition audits from an empty ledger rather "
        "than the one an earlier run left open (gke-labs/kube-agents#1023). The next "
        "audit run opens a fresh ledger; nothing here was resolved, and no run of the "
        "audit closed this."
    )


def reset(repo: str, project: str, build: str, token: str, audit_id: str | None, dry_run: bool) -> int:
    """Close the open ledgers; return how many did not close."""
    expected_repo(repo, project)
    scope = f"before repetition of the {audit_id} stream" if audit_id else "at lease time"
    ledgers = open_ledgers(repo, token, audit_id)
    unclosed = []
    for issue in ledgers:
        number = issue["number"]
        print(f"  #{number} {issue.get('title', '')}")
        if dry_run:
            continue
        # Each close stands alone: one that fails is reported and the rest
        # still close, because a ledger left open is the thing being fixed.
        try:
            api("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": closing_comment(build, scope)})
            api(
                "PATCH",
                f"/repos/{repo}/issues/{number}",
                token,
                {"state": "closed", "state_reason": CLOSE_REASON},
            )
        except (urllib.error.HTTPError, OSError) as exc:
            print(f"  #{number} did not close ({exc})", file=sys.stderr)
            unclosed.append(number)
    verb = "would close" if dry_run else "closed"
    what = f"open ledger(s) of the {audit_id} stream" if audit_id else "open ledger(s)"
    print(f"{verb} {len(ledgers) - len(unclosed)} {what} in {repo} {scope}")
    return len(unclosed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="owner/name of the leased project's GitOps repository")
    parser.add_argument("--project", required=True, help="the leased PROJECT_ID the repository must belong to")
    parser.add_argument("--build", required=True, help="the eval build id, named in the closing comment")
    parser.add_argument("--audit", default=None, help="close this stream's ledger only (the audit id, as in audit:<id>)")
    parser.add_argument("--dry-run", action="store_true", help="list what would close; write nothing")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        print(f"ERROR: {TOKEN_ENV} is not set; nothing to authenticate with", file=sys.stderr)
        return 2
    try:
        unclosed = reset(args.repo, args.project, args.build, token, args.audit, args.dry_run)
    except ResetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except urllib.error.HTTPError as exc:
        print(
            f"ERROR: GitHub answered HTTP {exc.code} ({exc.reason}) listing {args.repo}'s issues; "
            "a 403 or 404 here is the token's reach, not an empty repository",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"ERROR: could not reach api.github.com ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1
    return 1 if unclosed else 0


if __name__ == "__main__":
    sys.exit(main())
