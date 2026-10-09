"""The GitLab half of the eval's repository hygiene (kube-agents#2394).

Three callers share this: hack/ci_reset_audit_ledgers.py (close the open
ledger issues), hack/ci_reset_agent_pulls.py (close the agent's merge requests
and delete every branch but the default) and hack/ci_sweep_agent_pulls.py (the
same closes across the free pool projects, plus the token expiry warning),
each under `--forge gitlab`. The GitHub halves stay in those files; this one
mirrors their rules against GitLab's API shapes:

* one private project per pool project, `gke-agentic/<PROJECT_ID>-infra`
  (`gitlab_project_for_project()` in hack/ci-deploy.sh); `expected_project`
  refuses any other pair, as the GitHub guard does;
* the agent is the bot account the token belongs to (`kube-agents-eval-bot`
  today; read from the token, as the GitHub sweep reads the App's slug), an
  ordinary user with a personal access token (gitlab.com Free has no bot
  tokens), so "the agent's" means authored by that login and, for a merge
  request, from a branch in the project itself rather than a fork;
* a ledger is an open issue carrying `agent:audit` (and `audit:<id>` when a
  stream is named), titled `[audit] `, by the bot; it is closed with a note
  that opens with the GitHub reset's RESET_MARKER (the bench's GitLab ledger
  check, kube-agents#2433, reads it from the issue's notes as the GitHub one
  reads it from comments), then `state_event: close`;
* one token pair serves the whole pool and nothing here rotates it: GitLab's
  rotate call revokes the old token the moment it returns, with no overlap, so
  rotation is a human's yearly step (docs/ci-pool-projects.md 5.6) and the
  sweep's job is to say, through `token_expiry`, when that step is due.

Every token arrives through the environment or a function argument, never on
argv. `api` is the one network seam; the tests replace it.
"""

from __future__ import annotations

import datetime
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import ci_reset_audit_ledgers as ledgers

DEFAULT_HOST = "gitlab.com"
USER_AGENT = "kube-agents-ci-eval-gitlab"
REQUEST_TIMEOUT_SECONDS = 30
PER_PAGE = 100
MAX_PAGES = 20
# The one group that hosts every pool project, and the suffix the mapping in
# hack/ci-deploy.sh gives each (gitlab_project_for_project()).
GROUP = "gke-agentic"
PROJECT_SUFFIX = "-infra"
# The bot account the agent writes as. Issues and merge requests it did not
# author are a human's and stay.
BOT_LOGIN = "kube-agents-eval-bot"
AUDIT_LABEL = ledgers.AUDIT_LABEL
STREAM_LABEL_PREFIX = ledgers.STREAM_LABEL_PREFIX
TITLE_PREFIX = ledgers.TITLE_PREFIX
RESET_MARKER = ledgers.RESET_MARKER
AUDIT_REMEDIATION_LABEL = "audit:remediation"
STALE_CLOSED_LABEL = "audit:stale-closed"
# GitLab answers a branch that is already gone with 404; nothing else means gone.
REF_GONE_CODES = (404,)
WRITE_PAUSE_SECONDS = 1.0
# A token this close to its expiry is named by the sweep so CI health flags
# the yearly rotation while there is still time to do it with overlap.
EXPIRY_WARN_DAYS = 30
EXPIRY_URGENT_DAYS = 7
TOKEN_SELF_PATH = "/personal_access_tokens/self"

# A transient answer -- a 5xx, GitLab's 429, a connection that dropped or
# timed out -- is tried again, twice, 2 s then 8 s apart, as the GitHub pulls
# reset does: these fail closed, so one such answer would otherwise grade a
# repetition MISSING on a hiccup. A 429 waits what Retry-After asks, capped.
# Any other 4xx is the request's fault and is not retried.
RETRY_DELAYS_SECONDS = (2, 8)
RETRYABLE_STATUS_FLOOR = 500
TOO_MANY_REQUESTS_CODE = 429
RETRY_AFTER_HEADER = "Retry-After"
RETRY_AFTER_DEFAULT_SECONDS = 60
RETRY_AFTER_MAX_SECONDS = 120
CALL_FAULTS = (urllib.error.HTTPError, OSError, http.client.HTTPException)
ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class RateLimited(Exception):
    """GitLab refused the token under its limit twice over, or once with no
    attempt left to wait for. The caller ends its run rather than visiting
    every project through the cooldown, as the GitHub sweep does; a write
    loop lets this through where it swallows an ordinary fault."""

    def __init__(self, exc: urllib.error.HTTPError):
        super().__init__(f"GitLab answered HTTP 429 on {exc.url} with no retry left to wait; the token is rate-limited")
        self.cause = exc

pause = time.sleep
ResetError = ledgers.ResetError


def api_root(host: str = DEFAULT_HOST) -> str:
    return f"https://{host}/api/v4"


def expected_project(path: str, project: str) -> None:
    """Refuse unless `path` is the leased project's own GitLab project."""
    if not project:
        raise ResetError("no PROJECT_ID: refusing to touch a GitLab project for an unnamed lease")
    group, _, name = path.partition("/")
    if not group or not name or "/" in name:
        raise ResetError(f"{path!r} is not a group/project path")
    if group != GROUP:
        raise ResetError(f"{path} is not in the {GROUP} group, where every pool project lives; refusing to touch it")
    if name != project + PROJECT_SUFFIX:
        raise ResetError(
            f"{path} is not the GitLab project of the leased project {project} "
            f"(expected {GROUP}/{project}{PROJECT_SUFFIX}); refusing to touch it"
        )


def encoded(path: str) -> str:
    """A project path as GitLab's `:id`, URL-encoded whole (slash included)."""
    return urllib.parse.quote(path, safe="")


def api(method: str, path: str, token: str, body: dict | None = None, host: str = DEFAULT_HOST):
    """One GitLab call. Returns the decoded body, or None when it is empty."""
    data = None
    headers = {"PRIVATE-TOKEN": token, "Accept": "application/json", "User-Agent": USER_AGENT}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(api_root(host) + path, method=method, headers=headers, data=data)
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw = response.read()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise OSError(f"GitLab answered {method} {path} with a body that is not JSON: {exc}") from exc


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= RETRYABLE_STATUS_FLOOR or exc.code == TOO_MANY_REQUESTS_CODE
    return isinstance(exc, (OSError, http.client.HTTPException))


def _retry_after(exc: urllib.error.HTTPError, default: float) -> float:
    try:
        asked = int((exc.headers or {}).get(RETRY_AFTER_HEADER, "") or RETRY_AFTER_DEFAULT_SECONDS)
    except (TypeError, ValueError):
        asked = RETRY_AFTER_DEFAULT_SECONDS
    return float(min(max(asked, 1), RETRY_AFTER_MAX_SECONDS)) if exc.code == TOO_MANY_REQUESTS_CODE else default


def call(method: str, path: str, token: str, body: dict | None = None, host: str = DEFAULT_HOST):
    """One GitLab call, tried again after a transient answer: a 5xx or a
    dropped connection twice, a 429 once after its Retry-After and then
    RateLimited. `api` stays the raw seam the tests replace; this is what
    every read and write below uses."""
    limited_once = False
    for delay in (*RETRY_DELAYS_SECONDS, None):
        try:
            return api(method, path, token, body, host)
        except CALL_FAULTS as exc:
            # The 429 case is classified before the last-attempt test, so a
            # second 429 is RateLimited whatever came between the two.
            limited = isinstance(exc, urllib.error.HTTPError) and exc.code == TOO_MANY_REQUESTS_CODE
            if limited and (limited_once or delay is None):
                raise RateLimited(exc) from exc
            if delay is None or not _transient(exc):
                raise
            if limited:
                limited_once = True
                delay = _retry_after(exc, delay)
            print(f"  {method} {path} answered {exc}; trying again in {delay:g}s", file=sys.stderr)
            pause(delay)
    raise AssertionError("unreachable")


def _pages(path: str, query: dict, token: str, host: str, what: str) -> list[dict]:
    """Every item across the pages of a listing, in GitLab's order."""
    found: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        q = dict(query, per_page=str(PER_PAGE), page=str(page))
        batch = call("GET", f"{path}?{urllib.parse.urlencode(q)}", token, host=host)
        if not isinstance(batch, list) or not all(isinstance(item, dict) for item in batch):
            raise ResetError(f"GitLab answered {what} with a body that is not a list")
        if not batch:
            break
        found.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return found


def labels_of(item: dict) -> set[str]:
    """GitLab lists labels as names; the detail view of some objects as dicts."""
    names = set()
    for label in item.get("labels") or []:
        names.add(str(label.get("name") or "") if isinstance(label, dict) else str(label))
    return names


def author_of(item: dict) -> str:
    return str((item.get("author") or {}).get("username") or "")


def current_login(token: str, host: str = DEFAULT_HOST) -> str:
    """Whose token this is, read rather than written out, so a renamed or
    replaced bot account is followed instead of silently matching nothing (the
    GitHub sweep reads the App's slug from GET /app for the same reason)."""
    me = call("GET", "/user", token, host=host)
    login = str((me or {}).get("username") or "") if isinstance(me, dict) else ""
    if not login:
        raise ResetError("GitLab answered the token's user lookup without a username")
    return login


# --- ledgers ------------------------------------------------------------------


def not_a_ledger_because(issue: dict, audit_id: str | None, bot_login: str = BOT_LOGIN) -> str | None:
    """Why an issue is not a ledger, or None when it is one: the GitHub rule
    with the bot's login in place of the `[bot]` suffix."""
    labels = labels_of(issue)
    if AUDIT_LABEL not in labels:
        return f"no {AUDIT_LABEL} label"
    if audit_id:
        if STREAM_LABEL_PREFIX + audit_id not in labels:
            return f"no {STREAM_LABEL_PREFIX}{audit_id} label"
    elif not any(name.startswith(STREAM_LABEL_PREFIX) for name in labels):
        return f"no {STREAM_LABEL_PREFIX}<id> label"
    if not str(issue.get("title") or "").startswith(TITLE_PREFIX):
        return f"title does not start with {TITLE_PREFIX!r}"
    author = author_of(issue)
    if author != bot_login:
        return f"author {author or '?'} is not {bot_login}"
    return None


def open_ledgers(path: str, token: str, audit_id: str | None, host: str = DEFAULT_HOST, bot_login: str = BOT_LOGIN) -> list[dict]:
    """Every open ledger issue in the project, oldest first; a labelled issue
    that is not one is named on stdout with the reason it stays open."""
    labels = AUDIT_LABEL if not audit_id else f"{AUDIT_LABEL},{STREAM_LABEL_PREFIX}{audit_id}"
    issues = _pages(f"/projects/{encoded(path)}/issues", {"state": "opened", "labels": labels}, token, host, f"the issue listing for {path}")
    found = []
    for issue in issues:
        why = not_a_ledger_because(issue, audit_id, bot_login)
        if why is None:
            found.append(issue)
        else:
            print(f"  #{issue.get('iid', '?')} left open, not a ledger: {why}")
    found.sort(key=lambda issue: int(issue.get("iid") or 0))
    return found


def reset_ledgers(path: str, project: str, build: str, token: str, audit_id: str | None, dry_run: bool, host: str = DEFAULT_HOST) -> int:
    """Close the open ledgers; return how many did not close. The GitHub
    reset's contract, note for comment and `state_event` for the close."""
    expected_project(path, project)
    scope = f"before repetition of the {audit_id} stream" if audit_id else "at lease time"
    found = open_ledgers(path, token, audit_id, host, current_login(token, host))
    unclosed = []
    for issue in found:
        iid = issue["iid"]
        print(f"  #{iid} {issue.get('title', '')}")
        if dry_run:
            continue
        try:
            call("POST", f"/projects/{encoded(path)}/issues/{iid}/notes", token, {"body": ledgers.closing_comment(build, scope)}, host)
            call("PUT", f"/projects/{encoded(path)}/issues/{iid}", token, {"state_event": "close"}, host)
        except RateLimited:
            raise
        except CALL_FAULTS as exc:
            print(f"  #{iid} did not close ({exc})", file=sys.stderr)
            unclosed.append(iid)
    verb = "would close" if dry_run else "closed"
    what = f"open ledger(s) of the {audit_id} stream" if audit_id else "open ledger(s)"
    print(f"{verb} {len(found) - len(unclosed)} {what} in {path} {scope}")
    return len(unclosed)


# --- merge requests and branches ---------------------------------------------


def empty_record() -> dict:
    """The keys reset_merge_requests fills: hack/ci_reset_agent_pulls.py's
    record shape (new_record there), so the artifact reads the same on either
    forge; scripts/test_ci_gitlab_forge.py pins the two equal."""
    return {
        "open_before": 0, "kept_open": [], "labelled": [], "closed": [], "unclosed": [],
        "branches_before": 0, "deleted": [], "undeleted": [], "kept_branches": [],
        "open_after": None, "branches_after": None, "clean": False,
    }



def project_view(path: str, token: str, host: str = DEFAULT_HOST) -> tuple[int, str]:
    """(numeric id, default branch) of the project, both needed below."""
    payload = call("GET", f"/projects/{encoded(path)}", token, host=host)
    if not isinstance(payload, dict) or not payload.get("id") or not payload.get("default_branch"):
        raise ResetError(f"GitLab answered the project lookup for {path} without an id and a default branch")
    return int(payload["id"]), str(payload["default_branch"])


def open_merge_requests(path: str, token: str, host: str = DEFAULT_HOST) -> list[dict]:
    found = _pages(f"/projects/{encoded(path)}/merge_requests", {"state": "opened"}, token, host, f"the merge-request listing for {path}")
    found.sort(key=lambda mr: int(mr.get("iid") or 0))
    return found


def is_agent_merge_request(mr: dict, project_id: int, bot_login: str = BOT_LOGIN) -> bool:
    """Authored by the bot, from a branch in the project itself: only that
    branch is one the branch pass could delete from under a merge request."""
    return author_of(mr) == bot_login and int(mr.get("source_project_id") or 0) == project_id


def is_audit_merge_request(mr: dict) -> bool:
    return AUDIT_REMEDIATION_LABEL in labels_of(mr)


def branch_names(path: str, token: str, host: str = DEFAULT_HOST) -> list[str]:
    found = _pages(f"/projects/{encoded(path)}/repository/branches", {}, token, host, f"the branch listing for {path}")
    return [str(b.get("name") or "") for b in found if b.get("name")]


def _write(method: str, path: str, token: str, body: dict | None, host: str):
    try:
        return call(method, path, token, body, host)
    finally:
        pause(WRITE_PAUSE_SECONDS)


def branch_exists(path: str, name: str, token: str, host: str = DEFAULT_HOST) -> bool:
    """Whether the branch is there now, by the single-branch read. The branch
    LISTING lags a delete on gitlab.com (measured 2026-10-09 on a pool
    project: a deleted branch stayed listed for about 24 s while this read
    answered 404 at once), so the listing names candidates and this decides."""
    try:
        call("GET", f"/projects/{encoded(path)}/repository/branches/{urllib.parse.quote(name, safe='')}", token, host=host)
    except urllib.error.HTTPError as exc:
        if exc.code in REF_GONE_CODES:
            return False
        raise
    return True


def delete_branch(path: str, name: str, token: str, host: str = DEFAULT_HOST) -> None:
    try:
        _write("DELETE", f"/projects/{encoded(path)}/repository/branches/{urllib.parse.quote(name, safe='')}", token, None, host)
    except urllib.error.HTTPError as exc:
        if exc.code not in REF_GONE_CODES:
            raise


def _branches_in_use(mr: dict, project_id: int) -> set[str]:
    """What an open merge request that stays still needs here: its target
    always, its source when that is in the project itself."""
    names = {str(mr.get("target_branch") or "")}
    if int(mr.get("source_project_id") or 0) == project_id:
        names.add(str(mr.get("source_branch") or ""))
    return {n for n in names if n}


def reset_merge_requests(path: str, project: str, build: str, scope: str, token: str, dry_run: bool, record: dict, host: str = DEFAULT_HOST, bot_login: str | None = None) -> dict:
    """Close the bot's merge requests, delete every branch but the default,
    read back. Fills the caller's record (the GitHub reset's keys, so the
    artifact reads the same on either forge) and returns it. The bot is whoever
    the token belongs to unless the caller says otherwise."""
    record["forge"] = "gitlab"
    expected_project(path, project)
    bot_login = bot_login or current_login(token, host)
    project_id, default = project_view(path, token, host)
    mrs = open_merge_requests(path, token, host)
    agent = [mr for mr in mrs if is_agent_merge_request(mr, project_id, bot_login)]
    agent_iids = {mr.get("iid") for mr in agent}
    record["open_before"] = len(agent)
    heads_in_use: set[str] = set()
    for mr in mrs:
        if mr.get("iid") not in agent_iids:
            record["kept_open"].append(mr.get("iid"))
            print(f"  !{mr.get('iid', '?')} left open, not the agent's ({mr.get('source_branch', '')})")
            heads_in_use |= _branches_in_use(mr, project_id)
    for mr in agent:
        iid = mr["iid"]
        audit = is_audit_merge_request(mr)
        print(f"  !{iid} ({mr.get('source_branch', '')}){' audit, labelled first' if audit else ''}")
        if dry_run:
            continue
        # The label goes on before the close, never after: a close the audit
        # reads back unlabelled is a human's refusal to it.
        try:
            if audit:
                _write("PUT", f"/projects/{encoded(path)}/merge_requests/{iid}", token, {"add_labels": STALE_CLOSED_LABEL}, host)
                record["labelled"].append(iid)
            _write("PUT", f"/projects/{encoded(path)}/merge_requests/{iid}", token, {"state_event": "close"}, host)
        except RateLimited:
            raise
        except CALL_FAULTS as exc:
            print(f"  !{iid} did not close ({exc})", file=sys.stderr)
            record["unclosed"].append(iid)
            heads_in_use |= _branches_in_use(mr, project_id)
            continue
        record["closed"].append(iid)
    # The listing names the candidates; a name it still carries from a delete
    # seconds ago (an earlier repetition's reset, this run's own) is not a
    # branch, and must not read as one left behind.
    leftover = []
    for name in branch_names(path, token, host):
        if name == default:
            continue
        if not branch_exists(path, name, token, host):
            print(f"  branch {name} is listed but already gone (the listing lags a delete)")
            continue
        leftover.append(name)
    record["branches_before"] = len(leftover)
    for name in leftover:
        if name in heads_in_use:
            record["kept_branches"].append(name)
            print(f"  branch {name} kept, an open merge request has it as source")
            continue
        print(f"  branch {name}")
        if dry_run:
            continue
        try:
            delete_branch(path, name, token, host)
        except RateLimited:
            raise
        except CALL_FAULTS as exc:
            print(f"  branch {name} was not deleted ({exc})", file=sys.stderr)
            record["undeleted"].append(name)
            continue
        record["deleted"].append(name)
    if dry_run:
        print(f"would close {len(agent)} merge request(s) and delete {len(leftover) - len(record['kept_branches'])} branch(es) in {path} {scope}")
        return record
    after = open_merge_requests(path, token, host)
    still_agent = [mr["iid"] for mr in after if is_agent_merge_request(mr, project_id, bot_login)]
    heads_after: set[str] = set()
    for mr in after:
        heads_after |= _branches_in_use(mr, project_id)
    still_branches = [n for n in branch_names(path, token, host) if n != default and n not in heads_after and branch_exists(path, n, token, host)]
    record["open_after"] = len(still_agent)
    record["branches_after"] = len(still_branches)
    record["clean"] = not still_agent and not still_branches
    record["finished_at"] = time.strftime(ISO_UTC_FORMAT, time.gmtime(time.time()))
    print(
        f"closed {len(record['closed'])} merge request(s) and deleted {len(record['deleted'])} branch(es) in {path} {scope}; "
        f"{len(still_agent)} agent merge request(s) and {len(still_branches)} branch(es) remain"
    )
    return record


# --- the token ----------------------------------------------------------------


def token_expiry(token: str, host: str = DEFAULT_HOST, today: datetime.date | None = None) -> dict:
    """What the sweep reports about the pool's token: its name, scopes, expiry
    and the days left, and whether that is inside the warning window."""
    info = call("GET", TOKEN_SELF_PATH, token, host=host)
    if not isinstance(info, dict) or not info.get("expires_at"):
        raise ResetError("GitLab answered the token lookup without an expiry; every token here has one")
    today = today or datetime.date.today()
    expires = datetime.date.fromisoformat(str(info["expires_at"])[:10])
    days = (expires - today).days
    return {
        "name": str(info.get("name") or ""),
        "scopes": sorted(str(s) for s in info.get("scopes") or []),
        "expires_at": expires.isoformat(),
        "days_left": days,
        "active": bool(info.get("active", True)) and not info.get("revoked"),
        "warn": days <= EXPIRY_WARN_DAYS,
        "urgent": days <= EXPIRY_URGENT_DAYS,
    }


def expiry_message(entry: dict, secret: str) -> str | None:
    """The line CI health shows, or None while nothing is due."""
    if not entry.get("active", True):
        return f"GitLab token {entry['name']} ({secret}) is not active; create a new one (docs/ci-pool-projects.md 5.6)"
    if entry["warn"]:
        when = "urgently" if entry["urgent"] else "soon"
        return (
            f"GitLab token {entry['name']} ({secret}) expires {entry['expires_at']}, in {entry['days_left']} day(s): "
            f"rotate it {when}, with overlap (docs/ci-pool-projects.md 5.6)"
        )
    return None


# --- the preflight's probe ------------------------------------------------------

PROBE_TOKEN_ENV = "GITLAB_PROBE_TOKEN"


def whoami_main(argv: list[str] | None = None) -> int:
    """`python3 ci_gitlab_forge.py whoami [--host HOST]`: the eval preflight's
    authentication probe. The token arrives in GITLAB_PROBE_TOKEN, never on
    argv; the login it belongs to is printed on success, and a token that
    reads from Secret Manager but no longer authenticates (expired, revoked,
    replaced) is named on stderr with exit 1, so the run stops here instead of
    spending the lease to meet the same 401 in the resets."""
    import argparse

    parser = argparse.ArgumentParser(prog="ci_gitlab_forge.py whoami")
    parser.add_argument("--host", default=DEFAULT_HOST)
    args = parser.parse_args(argv)
    token = os.environ.get(PROBE_TOKEN_ENV, "")
    if not token:
        print("whoami: no token in %s" % PROBE_TOKEN_ENV, file=sys.stderr)
        return 2
    try:
        print(current_login(token, host=args.host))
        return 0
    except urllib.error.HTTPError as exc:
        why = "has expired or been revoked; create a new one" if exc.code == 401 else "cannot read its own user"
        print("the token no longer authenticates at %s (HTTP %d): it %s (docs/ci-pool-projects.md 5.6)" % (args.host, exc.code, why), file=sys.stderr)
        return 1
    except (RateLimited, ResetError, OSError, http.client.HTTPException, ValueError) as exc:
        print("the token could not be checked at %s: %s" % (args.host, exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if sys.argv[1:2] != ["whoami"]:
        print("usage: ci_gitlab_forge.py whoami [--host HOST]  (the token in %s)" % PROBE_TOKEN_ENV, file=sys.stderr)
        sys.exit(2)
    sys.exit(whoami_main(sys.argv[2:]))
