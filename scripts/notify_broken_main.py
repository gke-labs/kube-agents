#!/usr/bin/env python3
"""Track a required check failing on `main` as an automatically managed issue.

`main` breaks without anyone being told. #812 is the worked example: #790 bumped
`k8s.io/apimachinery`, #733 merged four hours later on a green check that had run
eighteen hours before the bump, and the golden fixtures the two commits disagreed
about took `Operator Tests` red. CI caught it immediately -- the push run on
`277de10` failed, and so did the next two commits that touched an operator path --
but nothing carried that anywhere a person would see it. The commits in between
reported success without compiling a line of Go, because `k8s-operator-test.yml`
skipped its own steps on a docs change: the push history on `main` reads red
(2654), green (2655), red (2657), red (2658), then four greens before the real
fix at 2700. It was three hours and forty-five minutes before anyone noticed.

So this writes it down. `.github/workflows/main-broken-notify.yml` hands it the
run id of a completed push run on `main` for one of the watched workflows, and
it decides what that run says:

    failing, previous run green     -> open an issue: "main is broken"
    failing, previous run failing   -> add the commit to that issue, and comment
    green                           -> close whatever issue is open, if any

One issue per breakage, not per failing run. A breakage that spans several merges
is one event, and keying the issue on the run that started the streak collapses
it into a single thing that opens, accumulates the commits that landed on top of
it, and closes itself when main recovers. The alternative -- an issue or a ping
per red run -- tells a reader about the break and never tells them it was fixed.

It reconciles state; it does not report transitions. That distinction is what
makes it survive its own delivery being unreliable. A green run closes any open
issue for its workflow whether or not the run before it was red, and the issue
body is rebuilt from the run history rather than appended to. So a notify run
that never happens -- GitHub keeps one run pending per concurrency group and
cancels the rest of a burst -- costs a comment, not a stuck issue; a re-run of a
red run that goes green closes the issue even though the history around it reads
green-after-green; and handling the same run twice cannot double a row.

A run whose news is already stale keeps quiet. If a later run of the same
workflow has finished by the time this one is handled, that later run is the
current state of main and this one would be announcing the past -- which, out of
order, means opening a "main is broken" issue against a main that is green.

What this trusts, and what has to hold for it to be right: a green run means the
tree is green. A workflow that reports `success` while covering only part of what
it is watched for breaks it -- that green closes the issue and names an innocent
commit as the fix. `k8s-operator-test.yml` did exactly that by skipping its own
steps on a docs change, and its push trigger now filters with `paths:` so a
commit it has nothing to say about produces no run rather than a false one.
`Prettier Check` cannot be fixed that way and is not watched; the watch list in
`main-broken-notify.yml` says why. Anything added to that list has to be checked
for the same shape, and this script cannot check it for you.

Conclusions outside `REPORTING_CONCLUSIONS` are ignored: `cancelled`, `skipped`,
`neutral`, `stale`, `action_required` are all a run declining to say anything,
and treating any of them as green would close an issue on a broken main.

Setup: none. It writes to this repository with the workflow's own `GITHUB_TOKEN`
and creates the `ci:main-broken` label the first time it needs it.

Run:  python3 scripts/notify_broken_main.py --run-id 123456789 --dry-run
Test: cd scripts && python3 -m unittest test_notify_broken_main
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from github_api import (
    API_ROOT,
    REQUEST_ATTEMPTS,
    REQUEST_RETRY_CEILING,
    REQUEST_RETRY_SECONDS,
    GitHubAPI as BaseGitHubAPI,
    _rate_limited,
    _retry_delay,
    log,
)

# How a run says "main does not build". `cancelled` and `skipped` are not here:
# the first is the concurrency group superseding a run, the second a path filter
# deciding there was nothing to do, and neither is a statement about the tree.
# `startup_failure` is -- it means the workflow file itself will not parse, which
# is as broken as a failing test and reaches main the same way.
FAILING_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})

# Conclusions that count as a run having reported on the tree at all. Anything
# outside this set is ignored when looking back for the previous run, so a
# cancelled run in the middle of a streak does not read as a recovery.
REPORTING_CONCLUSIONS = FAILING_CONCLUSIONS | frozenset({"success"})

# Runs of the triggering workflow to look back over. A streak longer than this
# would have its episode key fall off the end of the history and open a second
# issue -- an acceptable failure mode, given that fifty consecutive broken
# merges is a problem this script is not the answer to.
HISTORY_DEPTH = 50

LABEL = "ci:main-broken"
LABEL_COLOR = "d73a4a"
LABEL_DESCRIPTION = "A required check is failing on main"


# --------------------------------------------------------------------------- #
# Deciding whether there is anything to say
# --------------------------------------------------------------------------- #


def is_failing(run):
    return run["conclusion"] in FAILING_CONCLUSIONS


def reporting_history(runs, current):
    """`runs` newest-first, minus the current run and anything that said nothing.

    The API list includes the run that triggered this, and filtering it by id
    rather than by position matters: two merges land close enough together that
    the newest run is regularly not the one being handled.
    """
    return [
        run
        for run in runs
        if run["id"] != current["id"]
        and run["run_number"] < current["run_number"]
        and run["conclusion"] in REPORTING_CONCLUSIONS
    ]


def failure_streak(current, history):
    """The unbroken run of failures the current run sits at the end of.

    Oldest last, so `[-1]` is the run that broke main. For a recovery the
    current run is green and the streak is the one it just ended, so the current
    run is not part of it.
    """
    streak = [current] if is_failing(current) else []
    for run in history:
        if not is_failing(run):
            break
        streak.append(run)
    return streak


def decide(current, history):
    """What `current` says about main, or None if it says nothing.

    Three kinds, not four: there is no separate "recovered". Whether a green run
    is a recovery depends on whether an issue is open, which is a question for
    `reconcile` and the API -- not for the run history, which cannot see a
    notification that was dropped or a red run that was re-run into a green.

    Returns a dict rather than a class: it is rendered straight into an issue
    body and read straight out of the tests, and neither wants a constructor.
    """
    # A run that concluded `neutral`, `stale` or `action_required` reached here
    # because the workflow's `if:` only filters `cancelled` and `skipped`. None
    # of them is a statement about the tree, and anything not in
    # FAILING_CONCLUSIONS would otherwise be read as green and close the issue.
    if current["conclusion"] not in REPORTING_CONCLUSIONS:
        return None

    previous = history[0] if history else None
    streak = failure_streak(current, history)

    if is_failing(current):
        kind = "still-broken" if previous is not None and is_failing(previous) else "broken"
    else:
        kind = "green"

    return {
        "kind": kind,
        "run": current,
        # Empty only for a "broken" with nothing before it in the history, which
        # is the first run of a brand-new workflow. `broke_at` then falls back to
        # the current run, which is correct: it is the run that broke main.
        "broke_at": streak[-1] if streak else current,
        # Oldest first, which is the order the issue's table reads in.
        "streak": list(reversed(streak)),
        "streak_length": len(streak),
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _commit_subject(run):
    """The first line of the head commit's message, or the sha if there is none.

    A squash merge puts the pull request title here, which is the single most
    useful thing in the message -- it names the change that broke main.
    """
    message = ((run.get("head_commit") or {}).get("message") or "").strip()
    return message.splitlines()[0] if message else run["head_sha"][:7]


def _author(run):
    """Who wrote the change, which is not who pushed it.

    Nothing merges to main by hand here -- Prow's Tide does, so `actor` and
    `triggering_actor` are both `google-oss-prow[bot]` on every push and naming
    either tells a reader nothing. The squash commit keeps the pull request
    author (`Brad Hoekstra` on 277de10, with GitHub itself as committer), so
    that is the field to read. It is a display name and not a login, so it
    renders as plain text; the pull request link beside it is the one worth
    following.
    """
    name = ((run.get("head_commit") or {}).get("author") or {}).get("name")
    if name:
        return name
    for key in ("triggering_actor", "actor"):
        login = (run.get(key) or {}).get("login")
        if login:
            return login
    return "unknown"


# A squash merge ends its subject with the pull request number, which is the
# one link a reader of a broken-main issue actually wants: the change, its
# discussion, and its author are all one hop from there. Written bare (`#733`)
# so GitHub autolinks it -- which also leaves a back-reference on the pull
# request, pointing whoever broke main at the issue about it.
_PR_SUFFIX = re.compile(r"\(#(\d+)\)\s*$")


def _pull_request(run):
    match = _PR_SUFFIX.search(_commit_subject(run))
    return f"#{match.group(1)}" if match else ""


def _cell(text):
    """A table cell. Only `|` needs escaping -- it is the column separator, and
    a commit subject or an author's display name is free to contain one."""
    return text.replace("|", "\\|")


def _commit_link(run, repo):
    return f"[`{run['head_sha'][:7]}`](https://github.com/{repo}/commit/{run['head_sha']})"


def _provenance(run):
    """Where a commit came from: its run, its author, and its pull request."""
    where = f"[run {run['run_number']}]({run['html_url']}), by {_author(run)}"
    number = _pull_request(run)
    return f"{where} in {number}" if number else where


def episode_marker(notification, workflow_id):
    """The hidden line that ties an issue to one breakage of one workflow.

    Keyed on the run that started the streak, so every update about one
    breakage -- including the recovery that closes it -- finds the same issue,
    and the next breakage of the same workflow opens a fresh one. Matching on
    this rather than on the title means a renamed issue is still found.
    """
    return f"<!-- main-broken workflow={workflow_id} episode={notification['broke_at']['run_number']} -->"


def workflow_marker(workflow_id):
    """The prefix every episode marker for one workflow shares."""
    return f"<!-- main-broken workflow={workflow_id} "


def render_title(notification):
    return f"🔴 main is broken: {notification['run']['name']}"


def render_body(notification, repo, marker):
    """The issue body, rebuilt from scratch on every update.

    A table of the commits that have landed since main went red, oldest first.
    It is derived entirely from the run history, so it is idempotent: handling
    the same run twice produces the same body, and a hand-edited issue is
    restored by the next failure.
    """
    run = notification["run"]
    workflow = run["name"]
    streak = notification["streak"]

    lines = [f"🔴 **`{workflow}`** is failing on `main`.", ""]
    if len(streak) > 1:
        lines += [
            f"Broken since {_commit_link(streak[0], repo)} — {len(streak)} consecutive failures.",
            "",
        ]
    lines += ["| run | commit | author | PR |", "| --- | --- | --- | --- |"]
    for failed in streak:
        lines.append(
            f"| [{failed['run_number']}]({failed['html_url']}) "
            f"| {_commit_link(failed, repo)} "
            f"| {_cell(_author(failed))} "
            f"| {_pull_request(failed)} |"
        )
    lines += [
        "",
        f"Opened and closed automatically by "
        f"[`main-broken-notify.yml`](https://github.com/{repo}/blob/main/.github/workflows/main-broken-notify.yml). "
        f"It closes when `{workflow}` next passes on `main`; a failure after that opens a new issue.",
        "",
        marker,
    ]
    return "\n".join(lines)


def render_comment(notification, repo):
    """What to add to an existing issue, or None when the body says it all.

    A new issue needs no comment -- opening it is the notification. The other
    two kinds do: an issue body edit sends nobody anything, so without this a
    reader subscribed to the issue would learn neither that another commit had
    landed on a broken main nor that it had been fixed.

    For a green run this is rendered unconditionally and used only if an issue
    turns out to be open, which `reconcile` finds out and this cannot.
    """
    kind = notification["kind"]
    run = notification["run"]

    if kind == "green":
        broken_for = notification["streak_length"]
        if broken_for:
            plural = "" if broken_for == 1 else "s"
            ending = (
                f"Fixed by {_commit_link(run, repo)} — {_provenance(run)}. "
                f"{broken_for} consecutive failure{plural} before it."
            )
        else:
            # Green, with an issue open, and no failure immediately before it.
            # A dropped notify run, a red run re-run into a green, or two runs
            # handled out of order -- this run is not the fix and must not claim
            # to be, but main is green and the issue should not still be open.
            ending = (
                f"Green as of {_commit_link(run, repo)} — {_provenance(run)}. "
                f"This issue was still open; the runs it lists are not the current state of `main`."
            )
        return f"✅ `{run['name']}` passes on `main` again.\n\n{ending}"
    if kind == "still-broken":
        return (
            f"Still failing. {_commit_link(run, repo)} landed on a broken `main` — "
            f"{_provenance(run)}.\n\n"
            f"{notification['streak_length']} consecutive failures."
        )
    return None


# --------------------------------------------------------------------------- #
# GitHub API
# --------------------------------------------------------------------------- #


class GitHubAPI(BaseGitHubAPI):
    def __init__(
        self,
        repo,
        token,
        root=API_ROOT,
        user_agent="kube-agents-notify-broken-main",
        opener=urllib.request.urlopen,
        sleep=time.sleep,
    ):
        super().__init__(
            repo=repo,
            token=token,
            root=root,
            user_agent=user_agent,
            opener=opener,
            sleep=sleep,
        )

    def run(self, run_id):
        return self.get(f"/repos/{self.repo}/actions/runs/{run_id}")

    def history(self, workflow_id, branch="main", depth=HISTORY_DEPTH):
        """Completed push runs of one workflow on `branch`, newest first.

        `event=push` keeps pull-request runs of the same workflow out: they
        vastly outnumber the push runs and say nothing about main.
        """
        query = urllib.parse.urlencode(
            {
                "branch": branch,
                "event": "push",
                "status": "completed",
                "per_page": depth,
            }
        )
        path = f"/repos/{self.repo}/actions/workflows/{workflow_id}/runs?{query}"
        return self.get(path)["workflow_runs"]

    def open_issues_for_workflow(self, workflow_id):
        """Open `ci:main-broken` issues belonging to one workflow, newest first.

        The list endpoint returns pull requests too -- they are issues as far as
        this API is concerned -- so anything carrying a `pull_request` key is
        dropped. Deliberately unpaginated: more than a hundred open issues on
        this label is not a state worth writing code for.
        """
        query = urllib.parse.urlencode({"labels": LABEL, "state": "open", "per_page": 100})
        issues = self.get(f"/repos/{self.repo}/issues?{query}") or []
        prefix = workflow_marker(workflow_id)
        return [
            issue for issue in issues if "pull_request" not in issue and prefix in (issue.get("body") or "")
        ]

    def ensure_label(self):
        """Create the label if this is the first breakage ever recorded.

        422 is what GitHub returns for a label that already exists, which is the
        overwhelmingly common case and not an error.
        """
        self.request(
            "POST",
            f"/repos/{self.repo}/labels",
            {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION},
            tolerate=(422,),
        )

    def create_issue(self, title, body):
        return self.request("POST", f"/repos/{self.repo}/issues", {"title": title, "body": body, "labels": [LABEL]})

    def update_issue(self, number, **fields):
        return self.request("PATCH", f"/repos/{self.repo}/issues/{number}", fields)

    def comment(self, number, body):
        return self.request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def close_issue(self, number):
        return self.update_issue(number, state="closed", state_reason="completed")


# --------------------------------------------------------------------------- #
# Reconciling the issue with what the run history says
# --------------------------------------------------------------------------- #


def reconcile(api, notification, repo, workflow_id):
    """Bring the issues for this workflow into line with `notification`.

    Returns a human-readable account of what it did, for the log.
    """
    open_issues = api.open_issues_for_workflow(workflow_id)
    comment = render_comment(notification, repo)

    if notification["kind"] == "green":
        if not open_issues:
            # The overwhelmingly common case: main is green and nothing claims
            # otherwise. One list request per green run buys the guarantee that
            # an issue is never left open on a green main.
            return "green, and no issue is open for this workflow"
        # Every open issue for this workflow, not just the episode this run's
        # streak points at. Whatever the history says, main is green now, and an
        # issue saying otherwise is wrong.
        for issue in open_issues:
            api.comment(issue["number"], comment)
            api.close_issue(issue["number"])
        return "closed " + ", ".join(f"#{issue['number']}" for issue in open_issues)

    marker = episode_marker(notification, workflow_id)
    title = render_title(notification)
    body = render_body(notification, repo, marker)
    current = next((issue for issue in open_issues if marker in (issue.get("body") or "")), None)
    stale = [issue for issue in open_issues if issue is not current]

    if current is None:
        api.ensure_label()
        current = api.create_issue(title, body)
        done = f"opened #{current['number']}"
    else:
        api.update_issue(current["number"], title=title, body=body)
        if comment:
            api.comment(current["number"], comment)
        done = f"updated #{current['number']}"

    # An issue for an older breakage of this workflow is still open, which means
    # its recovery never got recorded. Point it at the current one and close it
    # rather than leaving two issues claiming main is broken.
    for issue in stale:
        api.comment(issue["number"], f"Superseded by #{current['number']}.")
        api.close_issue(issue["number"])
        done += f", superseded #{issue['number']}"
    return done


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", type=int, required=True, help="the completed workflow run to report on")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "gke-labs/kube-agents"),
        help="owner/name (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument("--branch", default="main", help="the branch whose health is being reported")
    parser.add_argument("--dry-run", action="store_true", help="print the issue instead of writing it")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("GITHUB_TOKEN (or GH_TOKEN) is not set")
        return 1

    api = GitHubAPI(args.repo, token)
    current = api.run(args.run_id)

    # The workflow's `if:` has already checked these, against the event payload.
    # Reading them back off the run is what makes a hand-run of this script on
    # the wrong run id harmless rather than an issue about a pull request.
    if current["head_branch"] != args.branch or current["event"] != "push":
        log(f"Run {args.run_id} is {current['event']} on {current['head_branch']}, not push on {args.branch}")
        return 0

    workflow_id = current["workflow_id"]
    runs = api.history(workflow_id, args.branch)

    # Notify runs are queued in the order the runs they watch *finish*, which is
    # not the order those runs started. Acting on a run that a later one has
    # already superseded announces the past: out of order, a red run handled
    # after the green that fixed it opens a "main is broken" issue against a
    # green main. The later run's own notify carries the whole story -- the body
    # is rebuilt from this same history -- so there is nothing lost by leaving
    # it to that one. Only a later run that said something counts; one filtered
    # out by the workflow's `if:` will never reconcile anything.
    superseded = [
        run
        for run in runs
        if run["run_number"] > current["run_number"] and run["conclusion"] in REPORTING_CONCLUSIONS
    ]
    if superseded:
        newest = max(run["run_number"] for run in superseded)
        log(f"Run {current['run_number']} is superseded by run {newest}; leaving it to that one")
        return 0

    notification = decide(current, reporting_history(runs, current))

    if notification is None:
        log(f"Run {current['run_number']} concluded {current['conclusion']}, which says nothing about main")
        return 0

    if args.dry_run:
        log(f"--dry-run: {notification['kind']}")
        if notification["kind"] != "green":
            marker = episode_marker(notification, workflow_id)
            log(f"\n# {render_title(notification)}\n\n{render_body(notification, args.repo, marker)}")
        comment = render_comment(notification, args.repo)
        if comment:
            log(f"\n--- comment, if an issue is open ---\n{comment}")
        return 0

    log(reconcile(api, notification, args.repo, workflow_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
