#!/usr/bin/env python3
"""Keep one tracking issue for each general overlay patch on main.

An overlay patch whose `Retire-When:` line starts with `upstream` holds a general fix:
`google/skills` must carry it, and a person must report it there. This script reads the
patches of the checkout and the tracking issues of the repository, then makes the issues
agree with the patches:

    patch on main               tracking issue   action
    general, soak window ended  none             open the issue
    general                     closed           comment, then open the issue again
    specific                    open             comment, then close as not planned
    gone                        open             comment, then close as completed

The identity of a tracking issue is the full path of the patch file, in a hidden marker
on a line of the issue body. The script finds its issues by the label and the marker. It
writes the title and the body one time, when it opens the issue; after that it changes
only the open or closed state. A run reads the state before it writes, so a second run
after a failure is safe. The request that opens an issue is sent one time only: after a
server error or a lost answer the run stops, and the next run reads the issues. If a
general patch has two open issues, the run names them and fails until a person closes one.

`.github/workflows/skill-upstream-tracking.yml` runs it. `--dry-run` prints the actions
and writes nothing: in a dry run the client refuses each request that is not a GET.
A run that writes refuses a ref that is not main.

Run: GITHUB_TOKEN=... python3 scripts/reconcile_skill_upstream.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import skill_overlay as overlay  # noqa: E402
from github_api import SERVER_ERROR_FLOOR  # noqa: E402
from github_api import GitHubAPI as BaseGitHubAPI  # noqa: E402
from github_api import is_refusal, log  # noqa: E402

DEFAULT_REPO = "gke-labs/kube-agents"
REPO_ENV = "GITHUB_REPOSITORY"
REF_ENV = "GITHUB_REF"
TOKEN_ENVS = ("GITHUB_TOKEN", "GH_TOKEN")
MAIN_BRANCH = "main"
MAIN_REF = f"refs/heads/{MAIN_BRANCH}"
SITE_ROOT = "https://github.com"
FILE_URL = SITE_ROOT + "/{repo}/blob/" + MAIN_BRANCH + "/{path}"
COMMIT_URL = SITE_ROOT + "/{repo}/commit/{sha}"
USER_AGENT = "kube-agents-reconcile-skill-upstream"
WORKFLOW_PATH = ".github/workflows/skill-upstream-tracking.yml"

# The first word of a Retire-When: line that passes `make skills-check`. Only the general
# words say that google/skills must carry the fix. The test file holds that each word the
# check accepts is in one of the two tuples.
GENERAL_WORDS = ("upstream", "upstream's")
SPECIFIC_WORDS = (overlay.RETIRE_WHEN_NEVER, "local")

# The tracking issue opens this many days after the commit that added the patch file. The
# window does not start again when the file changes.
SOAK_DAYS = 14
# The most issues that one run opens. main has 36 general patches at the first real run; a
# count above this limit is a wrong read of the tree or of the API, so the run stops and
# writes nothing.
MAX_ISSUES_OPENED = 40
# The time between two write actions. GitHub asks a client that sends many writes to wait
# one second or more between them; the first real run opens an issue for each old patch.
WRITE_PAUSE_SECONDS = 1

LABEL = "skills:upstream"
LABEL_COLOR = "0e8a16"
LABEL_DESCRIPTION = "An overlay patch holds a general fix to report to google/skills"
# needs-human: the task is not a code change, and agents skip an issue with this label.
# priority:p3: without a priority label, the daily triage sweep adds needs-triage.
EXTRA_LABELS = ("needs-human", "priority:p3")

# An author of this kind does not hold the task. The assignee is then the primary owner of
# the area below in docs/ownership.md.
# The test file holds that the list has each login of `options.robot_accounts` in
# .github/auto_request_review.yml.
ROBOT_LOGINS = ("kube-agents-robot", "kyber775")
BOT_LOGIN_SUFFIX = "[bot]"
OWNERSHIP_FILE = ("docs", "ownership.md")
OWNERSHIP_AREA = "Upstream skill intake (google/skills)"
OWNERSHIP_AREA_CELL = 1
OWNERSHIP_PRIMARY_CELL = 3
TABLE_CELL_SEPARATOR = "|"
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?")

MARKER = "<!-- skill-upstream-tracking path={path} -->"
# A body that a client saved with CRLF line ends has `\r` before the line end.
MARKER_RE = re.compile(r"^<!-- skill-upstream-tracking path=(\S+) -->[ \t\r]*$", re.MULTILINE)
PATH_SEPARATOR = "/"
# A marker path is <overlay root>/<skill>/<patch file>: the last two parts are these.
MARKER_PATH_TAIL = 2

ISSUE_OPEN = "open"
ISSUE_CLOSED = "closed"
ISSUE_STATE_ALL = "all"
CLOSE_COMPLETED = "completed"
CLOSE_NOT_PLANNED = "not_planned"
PULL_REQUEST_KEY = "pull_request"
HTTP_GET = "GET"
# GitHub answers 422 for a label that exists, and for an assignee that it refuses.
UNPROCESSABLE = 422

GENERAL = "general"
SPECIFIC = "specific"
UNREADABLE = "unreadable"

OPEN = "open"
REOPEN = "reopen"
CLOSE_SPECIFIC = "close-specific"
CLOSE_GONE = "close-gone"
WRITE_ACTIONS = (OPEN, REOPEN, CLOSE_SPECIFIC, CLOSE_GONE)
WAIT = "wait"
TRACKED = "tracked"
SKIP = "skip"

EXIT_OK = 0
EXIT_FAILURE = 1
SHORT_SHA = 9
NO_ASSIGNEE = "none"
DATE_FORMAT = "%Y-%m-%d"
UTC_SUFFIX = "Z"
UTC_OFFSET = "+00:00"

TITLE = "skills: report {skill}/{file} to google/skills"
BODY = """\
The overlay patch below holds a general fix. `google/skills` must carry the fix, so that this repository can retire the patch.

- Patch: [`{path}`]({patch_url})
- Added by: {commit_url} on {date}

The header of the patch gives the reason (`Why:`) and the upstream change that retires the patch (`Retire-When:`).

## Steps for the assignee

1. Search the open and closed issues of `google/skills` for this fix. One issue there can serve more than one patch.
2. If no issue exists, file one. `google/skills` takes issues and no outside pull requests.
3. Put the link to the `google/skills` issue in a comment on this issue.
4. When `google/skills` corrects the problem, run `make skills-sync SKILL={skill}` in its own pull request, with the eval loop.
5. If the sync stops on a conflict in this patch, resolve to the upstream text. The sync then drops the patch.
6. If the patch stays after the sync, delete it. Refer to "Remove a change" in the edit-mirrored-skill skill.
7. If `google/skills` declines the fix, open a pull request that changes the line to `Retire-When: never; <reason>`.

## Done when

- A sync or a pull request removes the patch file from `main`, or
- the `Retire-When:` line of the patch starts with `never;` or `local`.

Do not close this issue by hand. The workflow closes it when one of the two conditions is true. If a person closes it before that, the workflow opens it again.
Write in comments only, and do not change the last line of this text.

The workflow [`skill-upstream-tracking.yml`]({workflow_url}) opens and closes this issue.

{marker}
"""
REOPEN_COMMENT = (
    "The patch `{path}` is on `main`, and its `Retire-When:` line starts with `{word}`. "
    "Thus the report to `google/skills` is not done, and the workflow opens this issue again. "
    "The workflow closes the issue when one of the two \"Done when\" conditions is true."
)
CLOSE_SPECIFIC_COMMENT = (
    "The `Retire-When:` line of the patch `{path}` now starts with `{word}`. "
    "Thus the patch is specific to this repository, and `google/skills` has no fix to carry. "
    "The workflow closes this issue as not planned."
)
CLOSE_GONE_COMMENT = (
    "The patch file `{path}` is no longer on `main`. {removed} The workflow closes this issue as completed."
)
REMOVED_BY = "The commit that removed the file: {commit_url}."
REMOVED_BY_UNKNOWN = "The workflow could not read the commit that removed the file."


class DryRunWrite(RuntimeError):
    """A dry run tried a request that is not a GET."""


class Unanswered(Exception):
    """A request got a server error or no answer, so GitHub possibly did it. `cause` is the error."""

    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


class GitHubAPI(BaseGitHubAPI):
    def __init__(self, repo, token, dry_run=False, opener=urllib.request.urlopen, sleep=None):
        extra = {} if sleep is None else {"sleep": sleep}
        super().__init__(repo=repo, token=token, user_agent=USER_AGENT, opener=self._open, **extra)
        self.dry_run = dry_run
        self._send = opener
        self._no_resend = False

    def _open(self, request):
        """Send one request. While `_no_resend` is set, a server error or a lost answer ends the
        request: the shared client does not send it again by itself."""
        try:
            return self._send(request)
        except urllib.error.HTTPError as error:
            if self._no_resend and error.code >= SERVER_ERROR_FLOOR:
                raise Unanswered(error) from error
            raise
        except urllib.error.URLError as error:
            if self._no_resend:
                raise Unanswered(error) from error
            raise

    def request(self, method, path, payload=None, tolerate=()):
        if self.dry_run and method != HTTP_GET:
            raise DryRunWrite(f"a dry run does not send {method} {path}")
        return super().request(method, path, payload, tolerate=tolerate)

    def labelled_issues(self):
        """Each issue with the label, open and closed, on all pages. The list endpoint also
        returns pull requests; they are not tracking issues."""
        query = urllib.parse.urlencode({"labels": LABEL, "state": ISSUE_STATE_ALL})
        return [issue for issue in self.get_all(f"/repos/{self.repo}/issues?{query}") if PULL_REQUEST_KEY not in issue]

    def commits(self, path):
        """The commits on main that changed one path, newest first, on all pages. The script
        reads them from the API because the default checkout has no history."""
        query = urllib.parse.urlencode({"sha": MAIN_BRANCH, "path": path})
        return self.get_all(f"/repos/{self.repo}/commits?{query}")

    def ensure_label(self):
        self.post(
            f"/repos/{self.repo}/labels",
            {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION},
            tolerate=(UNPROCESSABLE,),
        )

    def create_issue(self, title, body, assignee):
        """Open a tracking issue. If GitHub refuses the assignee, the issue has none.

        After a server error or a lost answer, GitHub possibly made the issue, and a second
        request can open a second issue for the patch. So this request is sent one time
        only: the error ends the run, and the next run reads the issues. A rate limit is
        different: GitHub did nothing, and the shared client sends the request again.
        """
        path = f"/repos/{self.repo}/issues"
        payload = {"title": title, "body": body, "labels": [LABEL, *EXTRA_LABELS]}
        self._no_resend = True
        try:
            if assignee:
                try:
                    return self.post(path, {**payload, "assignees": [assignee]})
                except urllib.error.HTTPError as error:
                    if error.code != UNPROCESSABLE:
                        raise
                    log(f"GitHub refused the issue with the assignee {assignee}; the script sends it with no assignee")
            return self.post(path, payload)
        except Unanswered as error:
            raise error.cause from None
        finally:
            self._no_resend = False

    def comment(self, number, body):
        return self.post(f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def set_state(self, number, state, reason=None):
        fields = {"state": state}
        if reason:
            fields["state_reason"] = reason
        return self.patch(f"/repos/{self.repo}/issues/{number}", fields)


def first_word(path):
    """The first word of the one Retire-When: line. Call it only for a patch that passes the check."""
    header = overlay.patch_header(overlay.read_exact(path))
    line = next(line for line in header.splitlines() if line.startswith(overlay.RETIRE_WHEN_PREFIX))
    return line[len(overlay.RETIRE_WHEN_PREFIX):].strip().split(None, 1)[0]


def overlay_folders():
    """Each folder under the overlay root. A folder with no lock file counts too: a patch is
    gone only when its file is gone."""
    root = overlay.OVERLAY_ROOT
    return sorted(folder.name for folder in root.iterdir() if folder.is_dir()) if root.is_dir() else []


def patch_states():
    """{repository path: (kind, detail)} for each patch file of the checkout.

    The detail of a general or a specific patch is the first word of its Retire-When: line.
    A patch is unreadable, with the reason as its detail, if its Retire-When: line fails
    `make skills-check`, or if its path cannot be the identity of an issue.
    """
    states = {}
    for skill in overlay_folders():
        for patch in overlay.patches(skill):
            path = overlay.rel(patch)
            problem = overlay.retire_when_problem(patch)
            if marker_path(MARKER.format(path=path)) != path:
                # The next run cannot read a marker with this path, so it opens a new issue in each run.
                states[path] = (UNREADABLE, "the path cannot be the identity of an issue: a space or a name of a different form")
            elif problem:
                states[path] = (UNREADABLE, f"the Retire-When: line has a problem ({problem})")
            elif first_word(patch) in GENERAL_WORDS:
                states[path] = (GENERAL, first_word(patch))
            elif first_word(patch) in SPECIFIC_WORDS:
                states[path] = (SPECIFIC, first_word(patch))
            else:
                states[path] = (UNREADABLE, f"this script does not know the first word `{first_word(patch)}`")
    return states


def marker_path(body):
    """The patch path in the marker of an issue body. None if the body has no marker for a patch path."""
    found = MARKER_RE.findall(body or "")
    if not found:
        return None
    parts = found[-1].split(PATH_SEPARATOR)
    root = overlay.rel(overlay.OVERLAY_ROOT).split(PATH_SEPARATOR)
    if len(parts) != len(root) + MARKER_PATH_TAIL or parts[:-MARKER_PATH_TAIL] != root:
        return None
    skill, name = parts[-MARKER_PATH_TAIL:]
    if not (overlay.SKILL_NAME_RE.fullmatch(skill) and overlay.PATCH_SUBJECT_RE.fullmatch(name)):
        return None
    return found[-1]


def tracking_issues(api, notes):
    """({patch path: issue}, {patch path: [the other open issues]}).

    If more than one issue has the marker of a path, an open issue wins, then the oldest; a
    note names the others."""
    by_path = {}
    for issue in api.labelled_issues():
        path = marker_path(issue.get("body"))
        if path is None:
            notes.append(f"#{issue['number']} has the label {LABEL} and no marker of a patch path; not read")
            continue
        by_path.setdefault(path, []).append(issue)
    chosen, others_open = {}, {}
    for path, issues in by_path.items():
        issues.sort(key=lambda issue: (issue["state"] != ISSUE_OPEN, issue["number"]))
        chosen[path] = issues[0]
        others_open[path] = [issue for issue in issues[1:] if issue["state"] == ISSUE_OPEN]
        if len(issues) > 1:
            others = ", ".join(f"#{issue['number']}" for issue in issues[1:])
            notes.append(f"{path}: #{issues[0]['number']} is the tracking issue; {others} has the same marker")
    return chosen, others_open


def commit_time(commit):
    stamp = commit["commit"]["committer"]["date"]
    if stamp.endswith(UTC_SUFFIX):
        stamp = stamp[: -len(UTC_SUFFIX)] + UTC_OFFSET
    return datetime.datetime.fromisoformat(stamp)


def area_owner():
    """The primary owner of the skill intake area in docs/ownership.md. None if the row has no one login."""
    table = overlay.REPO_ROOT.joinpath(*OWNERSHIP_FILE)
    if not table.is_file():
        return None
    for line in table.read_text(encoding=overlay.TEXT_ENCODING).splitlines():
        cells = [cell.strip() for cell in line.split(TABLE_CELL_SEPARATOR)]
        if len(cells) > OWNERSHIP_PRIMARY_CELL and cells[OWNERSHIP_AREA_CELL] == OWNERSHIP_AREA:
            primary = cells[OWNERSHIP_PRIMARY_CELL]
            return primary if LOGIN_RE.fullmatch(primary) else None
    return None


def assignee_for(commit):
    """The author of the commit that added the patch. For a robot author, or a commit with
    no GitHub account, the area owner."""
    login = (commit.get("author") or {}).get("login")
    if not login or login in ROBOT_LOGINS or login.endswith(BOT_LOGIN_SUFFIX):
        return area_owner()
    return login


def decide(api, states, issues, others_open, now, duplicates):
    """One decision for each patch on main and each tracking issue: (kind, path, details).

    A patch that is gone or specific keeps no issue, so each open issue with its marker
    closes. A patch that keeps its issue and has more than one open issue gets a line in
    `duplicates`: the script closes no issue that it did not plan to close, so a person
    closes the others."""
    decisions = []
    for path in sorted(set(states) | set(issues)):
        kind, detail = states.get(path, (None, None))
        issue = issues.get(path)
        is_open = issue is not None and issue["state"] == ISSUE_OPEN
        open_issues = [issue, *others_open.get(path, [])] if is_open else []
        if kind is None:
            decisions.extend((CLOSE_GONE, path, {"issue": each}) for each in open_issues)
            continue
        if kind == SPECIFIC:
            decisions.extend((CLOSE_SPECIFIC, path, {"issue": each, "word": detail}) for each in open_issues)
            continue
        if len(open_issues) > 1:
            numbers = " and ".join(f"#{each['number']}" for each in open_issues)
            duplicates.append(f"{path}: {numbers} are open with the same marker; close all but one by hand")
        if kind == UNREADABLE:
            decisions.append((SKIP, path, {"reason": detail}))
        elif is_open:
            decisions.append((TRACKED, path, {"issue": issue}))
        elif issue is not None:
            decisions.append((REOPEN, path, {"issue": issue, "word": detail}))
        else:
            commits = api.commits(path)
            if not commits:
                decisions.append((SKIP, path, {"reason": "the API gave no commit for the file"}))
                continue
            added = commits[-1]
            ready = commit_time(added) + datetime.timedelta(days=SOAK_DAYS)
            if now < ready:
                decisions.append((WAIT, path, {"ready": ready}))
            else:
                decisions.append((OPEN, path, {"commit": added, "assignee": assignee_for(added)}))
    return decisions


def describe(kind, path, details):
    if kind == OPEN:
        commit = details["commit"]
        return (
            f"open     {path} (added {commit_time(commit).strftime(DATE_FORMAT)} by {commit['sha'][:SHORT_SHA]}, "
            f"assignee {details['assignee'] or NO_ASSIGNEE})"
        )
    if kind == REOPEN:
        return f"reopen   {path} (#{details['issue']['number']} is closed, and the patch is general)"
    if kind == CLOSE_SPECIFIC:
        return f"close    {path} (#{details['issue']['number']}, the patch is specific: `{details['word']}`)"
    if kind == CLOSE_GONE:
        return f"close    {path} (#{details['issue']['number']}, the patch file is gone)"
    if kind == WAIT:
        return f"wait     {path} (the soak window ends on {details['ready'].strftime(DATE_FORMAT)})"
    if kind == TRACKED:
        return f"tracked  {path} (#{details['issue']['number']} is open)"
    return f"skip     {path} ({details['reason']})"


def render_issue(repo, path, commit):
    skill, name = path.split(PATH_SEPARATOR)[-MARKER_PATH_TAIL:]
    title = TITLE.format(skill=skill, file=name)
    body = BODY.format(
        skill=skill,
        path=path,
        patch_url=FILE_URL.format(repo=repo, path=path),
        commit_url=COMMIT_URL.format(repo=repo, sha=commit["sha"]),
        date=commit_time(commit).strftime(DATE_FORMAT),
        workflow_url=FILE_URL.format(repo=repo, path=WORKFLOW_PATH),
        marker=MARKER.format(path=path),
    )
    return title, body


def apply(api, repo, kind, path, details):
    """Do one write action. A comment goes before the state change: a comment that fails after
    the change is never sent again, because the next run finds the state correct."""
    if kind == OPEN:
        title, body = render_issue(repo, path, details["commit"])
        api.create_issue(title, body, details["assignee"])
        return
    number = details["issue"]["number"]
    if kind == REOPEN:
        api.comment(number, REOPEN_COMMENT.format(path=path, word=details["word"]))
        api.set_state(number, ISSUE_OPEN)
    elif kind == CLOSE_SPECIFIC:
        api.comment(number, CLOSE_SPECIFIC_COMMENT.format(path=path, word=details["word"]))
        api.set_state(number, ISSUE_CLOSED, CLOSE_NOT_PLANNED)
    elif kind == CLOSE_GONE:
        # The newest commit of a path that is gone is the commit that removed the file. The
        # comment does not say that upstream carries the fix: a conflict in a sync, a
        # deletion by hand and a rename also remove the file, and main does not record the cause.
        commits = api.commits(path)
        removed = (
            REMOVED_BY.format(commit_url=COMMIT_URL.format(repo=repo, sha=commits[0]["sha"]))
            if commits
            else REMOVED_BY_UNKNOWN
        )
        api.comment(number, CLOSE_GONE_COMMENT.format(path=path, removed=removed))
        api.set_state(number, ISSUE_CLOSED, CLOSE_COMPLETED)


def reconcile(api, repo, now, dry_run):
    """Read the patches and the issues, print each decision, and do the write actions. Returns the exit code."""
    states = patch_states()
    if not states:
        log(f"no patch file under {overlay.rel(overlay.OVERLAY_ROOT)}: this is not a full checkout; nothing written")
        return EXIT_FAILURE
    notes, duplicates = [], []
    issues, others_open = tracking_issues(api, notes)
    decisions = decide(api, states, issues, others_open, now, duplicates)

    for note in notes:
        print(f"note     {note}")
    for duplicate in duplicates:
        print(f"double   {duplicate}")
    for kind, path, details in decisions:
        print(describe(kind, path, details))
    count = {
        kind: sum(1 for decision in decisions if decision[0] == kind) for kind in (*WRITE_ACTIONS, WAIT, TRACKED, SKIP)
    }
    general = sum(1 for kind, _ in states.values() if kind == GENERAL)
    specific = sum(1 for kind, _ in states.values() if kind == SPECIFIC)
    print(
        f"{len(states)} patches on {MAIN_BRANCH}: {general} general, {specific} specific, {count[SKIP]} not read. "
        f"Issues: {count[OPEN]} to open, {count[REOPEN]} to open again, "
        f"{count[CLOSE_SPECIFIC] + count[CLOSE_GONE]} to close, {count[TRACKED]} open and correct, "
        f"{count[WAIT]} in the soak window."
    )

    if count[OPEN] > MAX_ISSUES_OPENED:
        log(
            f"{count[OPEN]} issues to open is above the limit of {MAX_ISSUES_OPENED} for one run; nothing written. "
            f"If the count is correct, change MAX_ISSUES_OPENED in {Path(__file__).name}."
        )
        return EXIT_FAILURE
    writes = [decision for decision in decisions if decision[0] in WRITE_ACTIONS]
    if duplicates:
        log(f"{len(duplicates)} patches have more than one open issue; the run fails until a person closes the others")
    if dry_run:
        print(f"Dry run: {len(writes)} actions, nothing written.")
        return EXIT_FAILURE if duplicates else EXIT_OK
    if count[OPEN]:
        api.ensure_label()
    refused = 0
    for index, (kind, path, details) in enumerate(writes):
        if index:
            api.sleep(WRITE_PAUSE_SECONDS)
        try:
            apply(api, repo, kind, path, details)
        except urllib.error.HTTPError as error:
            # A refusal of one action on an issue that exists (a locked issue, for example)
            # does not stop the other actions. A refusal to open an issue does: the cause (a
            # missing permission, or a limit that GitHub does not mark) is the same for the
            # next one. A rate limit or a server error does too: more requests make it worse.
            if kind == OPEN or not is_refusal(error):
                raise
            refused += 1
            log(f"refused  {path}: GitHub answered {error.code} to the {kind} action")
    print(f"Done: {len(writes) - refused} actions." + (f" GitHub refused {refused}." if refused else ""))
    return EXIT_FAILURE if refused or duplicates else EXIT_OK


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get(REPO_ENV, DEFAULT_REPO), help=f"owner/name (default: ${REPO_ENV})")
    parser.add_argument("--ref", default=os.environ.get(REF_ENV, ""), help=f"the ref of the checkout (default: ${REF_ENV})")
    parser.add_argument("--dry-run", action="store_true", help="print the actions and write nothing")
    return parser.parse_args(argv)


def main(argv=None, opener=urllib.request.urlopen, now=None):
    args = parse_args(argv)
    if not args.dry_run and args.ref != MAIN_REF:
        log(f"a run that writes needs the ref {MAIN_REF}; this ref is `{args.ref}`. Use --dry-run on a different ref.")
        return EXIT_FAILURE
    token = next((os.environ[name] for name in TOKEN_ENVS if os.environ.get(name)), None)
    if not token:
        log(f"{' or '.join(TOKEN_ENVS)} is not set")
        return EXIT_FAILURE
    api = GitHubAPI(args.repo, token, dry_run=args.dry_run, opener=opener)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    try:
        return reconcile(api, args.repo, now, args.dry_run)
    except (urllib.error.URLError, DryRunWrite) as error:
        log(f"the run stopped: {error}")
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
