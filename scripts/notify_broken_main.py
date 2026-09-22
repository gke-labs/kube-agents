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
run id of a completed push run on `main` for one of the watched workflows -- or,
on a schedule or a dispatch, nothing -- and it decides what that workflow's
newest completed run that said anything says:

    failing, previous run green     -> open an issue: "main is broken"
    failing, previous run failing   -> add the commit to that issue, and comment
    green                           -> close any older issue still open

One issue per breakage, not per failing run. A breakage that spans several merges
is one event, and keying the issue on the run that started the streak collapses
it into a single thing that opens, accumulates the commits that landed on top of
it, and closes itself when main recovers. The alternative -- an issue or a ping
per red run -- tells a reader about the break and never tells them it was fixed.

It reconciles state; it does not report transitions. Whichever run woke it, it
reads the workflow's completed push runs back from the API and brings the issues
into line with the newest one that said anything. The triggering run is only the
reason to look: if a later run has finished by the time this one is handled, the
later run is the current state of main and is the one acted on. A green closes
any open issue about an older red whether or not the run before it was red, the
issue body is rebuilt from the run history rather than appended to, and an issue
whose body already says what the history says is left alone. So a re-run of a
red run that goes green closes the issue even though the history around it reads
green-after-green; handling the same run twice writes nothing the second time;
and two runs handled out of order cannot open a "main is broken" issue against a
main that is green.

Two bounds keep the reconciliation from undoing a person or a newer run. A
green closes only issues whose episode is no newer than the green run itself,
because an issue for a red that finished after this green describes a main this
green has not seen -- and never one a person reopened, which GitHub marks as
such. And a closed issue that carries the current episode's marker
and was closed after the last change to any run in the streak is a breakage
that has been dealt with, whoever closed it -- a person, or a merge whose
description named the issue -- so it is not filed again for evidence that
predates the close. A run re-run after the close, or a new red, moves the streak past it and
files as usual, and so does the next episode. No close made on this workflow's
own token ever counts: its "superseded" says another issue covers the breakage,
not that anyone dealt with it, and its "recovered" is protected another way --
the issue is stamped with a hidden `fixed-by` line naming the green, so a later
page that lacks that green reads as a hole rather than as a relapse to dismiss.
(A "superseded" close is stamped `superseded-by`; a state reason could not tell
the two apart from a person's, since "not planned" is also what a person picks
for a false alarm.) Trusting an own close would let a page that still shows a
re-run green close a real red and then silence it.

A third bound is about the read rather than the state. The run-history endpoint
has answered with a page missing recent runs, and once with a page weeks old:
on 2026-09-17 a read that lacked runs 5928 and 6008 made run 6009 look like a
fresh breakage, and the notifier opened a second issue and closed the right one
as superseded. So before it opens, supersedes, rewrites or closes anything, it
checks that every run the issues in play name -- open, and closed when it is
about to open one -- is in the list it read, or is older than a full page, or,
for a run the issue links to, has been deleted from GitHub, which are the
honest reasons for a run to be absent; and an empty page for a workflow the
endpoint itself counts runs for is refused before any of that -- only the
empty one, since the count has been seen to change between two calls and a
short page is the ordinary state of a newly watched workflow. Issues name only
red runs, so a green close also stamps the issue with the run that fixed it: a
later page that has the reds around that green but not the green itself is
then a hole too, not a relapse. A read that fails writes nothing; the next read
is at most fifteen minutes away, or as long as a re-run of a listed run takes,
since a run being re-run is not completed and is absent from every page until
it is.

That is also what makes a dropped notify run survivable. GitHub keeps one run
pending per concurrency group and cancels the rest of a burst, and the earlier
design let each run speak only for itself, deferring to any later run: on
2026-09-16 six merges landed within a minute, the only red run of the burst
(#1651, `b458323d`) finished before two of the greens, the group cancelled two
notify runs in the seconds after it -- on the timing, its own among them -- and
every surviving run deferred to it. Nothing was filed for nearly fifteen hours,
until the next merge produced a fresh run; #1681 is the write-up. Now any run of
the burst reconciles against that red, and the last arrival in a group is never
cancelled -- so a burst costs nothing as long as one event is delivered.

Delivery can still fail entirely: a `workflow_run` event that is never sent, a
runner outage. `--sweep` does the same reconciliation for every workflow in
`WATCHED_WORKFLOWS` with no run to start from, and the workflow runs it on a
schedule. It is a backstop, not the primary path -- what it costs is a bounded
delay -- and it is safe to run at any time because a reconciliation that finds
nothing to change writes nothing.

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
      python3 scripts/notify_broken_main.py --sweep --dry-run
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
from pathlib import Path

from github_api import (
    API_ROOT,
    PER_PAGE,
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

# Completed push runs of a watched workflow to look back over. A streak longer
# than this would have its episode key fall off the end of the history, open a
# second issue and close the first as superseded -- an acceptable failure mode,
# given that fifty consecutive broken merges is a problem this script is not
# the answer to.
HISTORY_DEPTH = 50

# The workflows `--sweep` reconciles, by the `name:` each declares -- the same
# key `main-broken-notify.yml` matches its `workflow_run` trigger on. A workflow
# cannot read its own triggers, so the list exists three times: the trigger,
# this tuple (`test_notify_broken_main.py` fails when the two differ), and
# `BROKEN_MAIN_WATCHED_WORKFLOWS` in `test_integration_contracts.py`, the
# roster of required checks the trigger must contain. The workflow's header says
# what may go on the list and why `Prettier Check` is not on it.
WATCHED_WORKFLOWS = (
    "Actionlint",
    "Docker Build",
    "Documentation Checks",
    "Operator Tests",
    "Python Unit Tests",
    "Validate Repo Structure",
)

LABEL = "ci:main-broken"
LABEL_COLOR = "d73a4a"
LABEL_DESCRIPTION = "A required check is failing on main"

# Status codes that separate GitHub refusing a request from GitHub failing:
# the first is ours to fix and reds the sweep, the second is retried later.
HTTP_SERVER_ERROR_FLOOR = 500
HTTP_TOO_MANY_REQUESTS = 429

# Where the checked-out repository is, relative to this file: the sweep runs
# from a checkout of the default branch, so a workflow file that exists here is
# the live one and a listed path that does not is a ghost.
REPO_ROOT = Path(__file__).resolve().parent.parent

# The two issue states the reconciliation reads.
ISSUE_OPEN = "open"
ISSUE_CLOSED = "closed"

# What GitHub records on an issue a person reopened. A green leaves such an
# issue alone: reopening is a decision, and the sweep would otherwise close it
# again every fifteen minutes with a fresh "passes again" comment each time.
REOPENED = "reopened"

# The state reason every close carries. Not used to mean anything: "not
# planned" is what a person picks for a false alarm, so a close's meaning is
# stamped into the body instead (below).
CLOSE_COMPLETED = "completed"

# Who closes an issue when this script does, on the workflow's own token. No
# close by this login counts as a dismissal: a "superseded" says another issue
# covers the breakage, and a "recovered" is protected by its `fixed-by` stamp
# instead, since a page that still shows a re-run green could otherwise close
# a real red and have that close silence it.
OWN_CLOSER_LOGIN = "github-actions[bot]"

# Hidden lines appended to an issue's body as it is closed. `superseded-by`
# says another issue covers the breakage, so that close is never read as
# someone dealing with it; `fixed-by` names the green run that ended the
# episode, so a later history read that lacks that run reads as a hole.
SUPERSEDED_STAMP = "<!-- main-broken superseded-by={number} -->"
SUPERSEDED_PREFIX = "<!-- main-broken superseded-by="
FIXED_BY_STAMP = "<!-- main-broken fixed-by={number} run={run_id} -->"

# The episode marker, read back off an issue body to learn which run opened it;
# a table row, read back to learn which runs the issue already lists (its link
# is the run's `html_url` verbatim, which for a re-run may end in
# `/attempts/N`); and the fixed-by stamp, read back to learn which green run
# closed it.
_EPISODE = re.compile(r"<!-- main-broken workflow=\d+ episode=(\d+) -->")
_ROW_RUN = re.compile(r"^\| \[(\d+)\]\(\S*?/actions/runs/(\d+)(?:/attempts/\d+)?\)", re.MULTILINE)
_FIXED_BY = re.compile(r"<!-- main-broken fixed-by=(\d+) run=(\d+) -->")

# A refusal to write is a log line and exit 0, since a stale page is ordinary
# and a red job for one would be noise; this prefix makes the line a warning
# annotation on the job, so a refusal that persists shows up in the summary.
# Emitted only when the notify workflow asks for it, through a variable of its
# own: the unit tests exercise every refusal, and their output is echoed into a
# CI step where `GITHUB_ACTIONS` is set too, so gating on that would annotate
# every pull request's test job instead.
WARNING_PREFIX = "::warning::"
ANNOTATE_ENV = "MAIN_BROKEN_ANNOTATE"


# --------------------------------------------------------------------------- #
# Deciding whether there is anything to say
# --------------------------------------------------------------------------- #


def warn(message):
    """A log line that, when the workflow asks, also annotates the job. Stdout,
    where the runner reads workflow commands."""
    if os.environ.get(ANNOTATE_ENV):
        print(f"{WARNING_PREFIX}{message}", flush=True)
    log(message)


def is_failing(run):
    return run["conclusion"] in FAILING_CONCLUSIONS


def newest_reporting_run(runs):
    """The completed run that is the current state of main, or None.

    Newest by `run_number`, not by position or by completion time: the list is
    ordered by creation, and a burst of merges finishes out of order -- a red
    run that fails fast completes before the greens queued ahead of it. A run
    that concluded `cancelled`, `skipped`, `neutral` or the like is passed over,
    since it said nothing about the tree and the run before it still stands.
    """
    reporting = [run for run in runs if run["conclusion"] in REPORTING_CONCLUSIONS]
    return max(reporting, key=lambda run: run["run_number"], default=None)


def reporting_history(runs, current):
    """`runs` newest-first, minus the current run and anything that said nothing.

    `current` is the newest reporting run, so every other reporting run is
    older; the list may hold newer runs that said nothing, and the conclusion
    filter drops those. Sorted, because the streak is read off the order and
    the list's order is only as good as the API's, plus whatever `main` put
    back in.
    """
    history = [
        run for run in runs if run["id"] != current["id"] and run["conclusion"] in REPORTING_CONCLUSIONS
    ]
    return sorted(history, key=lambda run: run["run_number"], reverse=True)


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
    # `report` only ever passes a reporting run, so this is a guard for a
    # direct caller: a run that concluded `cancelled`, `neutral`, `stale` or
    # the like is no statement about the tree, and anything not in
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


def episode_of(issue):
    """The run number an issue's marker says broke main, or 0 if it has none.

    Zero rather than None so an issue with a damaged marker still compares as
    older than any run and is closed by the next green rather than kept open.
    """
    match = _EPISODE.search(issue.get("body") or "")
    return int(match.group(1)) if match else 0


def runs_named(issue):
    """Every run an issue names -- marker, table rows, fixed-by stamp -- as run
    number to run id.

    The id comes from a row's link or a stamp's `run=`; the marker carries
    only the number, so an episode with no row of its own maps to None.
    """
    body = issue.get("body") or ""
    named = {int(number): int(run_id) for number, run_id in _ROW_RUN.findall(body)}
    for number, run_id in _FIXED_BY.findall(body):
        named.setdefault(int(number), int(run_id))
    episode = episode_of(issue)
    if episode:
        named.setdefault(episode, None)
    return named


def listed_rows(issue):
    """The run numbers an issue's table lists, which is what counts as news."""
    return {int(number) for number, _ in _ROW_RUN.findall(issue.get("body") or "")}


def _green_that_ended(issue, page):
    """The first green after an issue's last listed red on the page in hand
    -- the run that ended its episode -- or None if the page shows no green
    after it (the issue is a duplicate of the current episode, whose streak
    runs to the end of the page) or does not reach back to the issue at all.

    Reds between the last listed row and that green are the same episode: an
    issue's table lags its streak whenever the notify runs that would have
    added rows were cancelled, which is the burst this exists for.
    """
    last_red = max(listed_rows(issue) | {episode_of(issue)}, default=0)
    if not any(run["run_number"] <= last_red for run in page):
        return None
    later = sorted(
        (run for run in page if run["run_number"] > last_red and run["conclusion"] in REPORTING_CONCLUSIONS),
        key=lambda run: run["run_number"],
    )
    return next((run for run in later if not is_failing(run)), None)


def history_window(runs):
    """What one read of the history covered, for judging whether it is whole.

    A full page may honestly omit runs older than its oldest entry; a page
    shorter than `HISTORY_DEPTH` claims to be the workflow's entire history
    and may omit nothing at all.
    """
    numbers = {run["run_number"] for run in runs}
    return {"numbers": numbers, "oldest": min(numbers, default=0), "full": len(runs) >= HISTORY_DEPTH}


def unlisted_runs(issue, window):
    """Runs an issue names that a whole read would list and this one did not.

    Non-empty means the read is missing runs known to exist, and any decision
    built on it -- a fresh episode, a shorter streak, a recovery -- is built
    on a hole. The reconciliation then writes nothing. Run number to run id,
    so the caller can ask GitHub whether the run still exists at all.
    """
    return {
        number: run_id
        for number, run_id in runs_named(issue).items()
        if number not in window["numbers"] and (not window["full"] or number >= window["oldest"])
    }


def render_title(notification):
    return f"🔴 main is broken: {notification['run']['name']}"


def render_body(notification, repo, marker):
    """The issue body, rebuilt from scratch on every update.

    A table of the commits that have landed since main went red, oldest first.
    It is derived entirely from the run history, so it is idempotent: handling
    the same run twice produces the same body. It is written only when the set
    of rows changes -- a commit landing on the broken main, or a listed run
    deleted from GitHub -- so a note someone adds to the body stays until then.
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
        """One run as GitHub has it now, or None if it has been deleted."""
        return self.get(f"/repos/{self.repo}/actions/runs/{run_id}", tolerate=(404,))

    def workflows(self):
        """Every workflow the repository has, paged to the endpoint's own total.

        The list endpoint returns `{"total_count": n, "workflows": [...]}`, so
        the shared client's `get_all`, which pages a bare list, cannot read it.
        An empty page ends the loop as well as reaching the total, so a total
        that overstates the list cannot spin this.
        """
        found = []
        page = 1
        while True:
            query = urllib.parse.urlencode({"per_page": PER_PAGE, "page": page})
            batch = self.get(f"/repos/{self.repo}/actions/workflows?{query}")
            found.extend(batch["workflows"])
            if not batch["workflows"] or len(found) >= batch["total_count"]:
                return found
            page += 1

    def run_exists(self, run_id):
        """Whether a run is still there. An administrator can delete one."""
        return self.run(run_id) is not None

    def history(self, workflow_id, branch="main", depth=HISTORY_DEPTH):
        """Completed push runs of one workflow on `branch`, newest first, or
        None when the page is empty although the endpoint's own count says
        the workflow has runs -- a read not to build anything on. Only that
        case: the count has been seen to differ between two calls minutes
        apart, so a page merely shorter than it is not evidence, and a newly
        watched workflow has fewer runs than a page for a while.

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
        page = self.get(path)
        runs = page["workflow_runs"]
        if not runs and page.get("total_count", 0) > 0:
            warn(f"history of workflow {workflow_id} came back empty for {page['total_count']} runs; not trusting it")
            return None
        return runs

    def issues_for_workflow(self, workflow_id, state):
        """`ci:main-broken` issues in `state` belonging to one workflow, newest first.

        The list endpoint returns pull requests too -- they are issues as far as
        this API is concerned -- so anything carrying a `pull_request` key is
        dropped. Deliberately one page: more than a hundred open issues on this
        label is not a state worth writing code for, and the closed list is
        read for the newest closed issues only -- a dismissal of an episode
        still in progress, and runs a short page failed to list -- so an older
        closed issue past the page goes unchecked, which errs toward writing.
        Read afresh on every call, never cached across a sweep: a list read
        while one workflow was reconciled is stale for the next, and a write
        against it could rewrite an issue a green has since closed.
        """
        prefix = workflow_marker(workflow_id)
        query = urllib.parse.urlencode({"labels": LABEL, "state": state, "per_page": PER_PAGE})
        issues = self.get(f"/repos/{self.repo}/issues?{query}") or []
        return [
            issue for issue in issues if "pull_request" not in issue and prefix in (issue.get("body") or "")
        ]

    def issue(self, number):
        """One issue as GitHub has it now, for a write that must not be made
        against a list read minutes ago."""
        return self.get(f"/repos/{self.repo}/issues/{number}")

    def stamp(self, issue, stamp):
        """Append a hidden line to an issue's body, re-read first, state kept."""
        fresh = self.issue(issue["number"]) or issue
        return self.update_issue(issue["number"], body=(fresh.get("body") or "").rstrip() + "\n" + stamp)

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

    def close_issue(self, issue, stamp, comment=None):
        """Comment on an issue, then close it with its body stamped in one
        write; False, and nothing written, if it was already closed.

        The issue is re-read first: the list it came from may be minutes old
        by now, so a note someone wrote in the meantime must not be
        overwritten, and a sweep and an event run reaching the same green
        together must not both close and comment. The comment goes before the
        close, not after: a comment that fails after a close has landed would
        never be retried, since no later reconciliation finds the issue open,
        and a reader told main was broken would never be told it was fixed. A
        close that fails after the comment is retried by the next
        reconciliation, at the cost of a repeated comment.
        """
        fresh = self.issue(issue["number"]) or issue
        if fresh.get("state") == ISSUE_CLOSED:
            return False
        if comment:
            self.comment(issue["number"], comment)
        body = (fresh.get("body") or "").rstrip() + "\n" + stamp
        self.update_issue(issue["number"], body=body, state=ISSUE_CLOSED, state_reason=CLOSE_COMPLETED)
        return True


# --------------------------------------------------------------------------- #
# Reconciling the issue with what the run history says
# --------------------------------------------------------------------------- #


def reconcile(api, notification, repo, workflow_id):
    """Bring the issues for this workflow into line with `notification`.

    Returns a human-readable account of what it did, for the log.
    """
    open_issues = api.issues_for_workflow(workflow_id, ISSUE_OPEN)
    comment = render_comment(notification, repo)
    window = notification["window"]

    deleted = {}

    def still_exists(run_id):
        """A run the read lacks may have been deleted, which is no hole."""
        if run_id is None:
            return True
        if run_id not in deleted:
            deleted[run_id] = not api.run_exists(run_id)
        return not deleted[run_id]

    def incomplete_for(issues):
        """The issues among `issues` that name runs this read did not list."""
        gaps = {}
        for issue in issues:
            missing = sorted(n for n, run_id in unlisted_runs(issue, window).items() if still_exists(run_id))
            if missing:
                gaps[issue["number"]] = missing
        return gaps

    def leave(gaps):
        described = "; ".join(f"#{number} names run(s) {', '.join(map(str, missing))}" for number, missing in gaps.items())
        message = f"the history read is missing runs that exist ({described}); writing nothing until a fuller read"
        warn(message)
        return message

    if notification["kind"] == "green":
        # The green that ends a streak stamps the streak's issue with the run
        # that fixed it, so a later page lacking this green reads as a hole. An
        # issue a person or a "Fixes #N" merge closed before this run finished
        # is already closed and would otherwise never carry the stamp; it is
        # stamped here, closed as it is.
        stamped = []
        if notification["streak"]:
            fix_stamp = FIXED_BY_STAMP.format(number=notification["run"]["run_number"], run_id=notification["run"]["id"])
            ended = episode_marker(notification, workflow_id)
            unstamped = [
                issue
                for issue in api.issues_for_workflow(workflow_id, ISSUE_CLOSED)
                if ended in (issue.get("body") or "")
                and not _FIXED_BY.search(issue.get("body") or "")
                and SUPERSEDED_PREFIX not in (issue.get("body") or "")
            ]
            # A stamp is a claim about this page; a page with holes makes none.
            gaps = incomplete_for(unstamped)
            if gaps:
                return leave(gaps)
            for issue in unstamped:
                api.stamp(issue, fix_stamp)
                stamped.append(issue)
        stamped_note = "; stamped " + ", ".join(f"#{issue['number']}" for issue in stamped) if stamped else ""
        if not open_issues:
            # The overwhelmingly common case: main is green and nothing claims
            # otherwise. One list request per green run buys the guarantee that
            # an issue about an older red is never left open on a green main a
            # whole read has seen.
            return "green, and no issue is open for this workflow" + stamped_note
        # Every open issue for this workflow whose breakage this green run comes
        # after, not just the episode this run's streak points at: whatever the
        # history says, main is green as of this run, and an issue about an
        # older red is wrong. An issue about a red that ran *after* this green
        # is not -- a sweep that read the history a moment before that red
        # finished, or read a list that lagged it, would otherwise close a
        # correct issue with a "passes again" naming a run that is not the fix.
        green_number = notification["run"]["run_number"]
        reopened = [issue for issue in open_issues if issue.get("state_reason") == REOPENED]
        closable = [
            issue for issue in open_issues if issue not in reopened and episode_of(issue) <= green_number
        ]
        newer = [issue for issue in open_issues if issue not in closable and issue not in reopened]
        gaps = incomplete_for(closable)
        if gaps:
            return leave(gaps)
        # A page can carry a conclusion a re-run has since changed. If an issue
        # about to be closed lists this green run as a red row, the run has
        # been red since that issue was written; ask GitHub what it is now
        # before closing on a page that may predate the re-run.
        if any(green_number in listed_rows(issue) for issue in closable):
            fresh = api.run(notification["run"]["id"])
            if fresh is not None and fresh.get("conclusion") != notification["run"]["conclusion"]:
                message = (
                    f"run {green_number} reads {notification['run']['conclusion']} on this page "
                    f"but {fresh.get('conclusion')} now; writing nothing until a fuller read"
                )
                warn(message)
                return message
        # The close re-reads the issue and says whether it was still open, so
        # two reconciliations reaching this green together produce one
        # comment rather than two.
        closed = []
        for issue in closable:
            if api.close_issue(issue, FIXED_BY_STAMP.format(number=green_number, run_id=notification["run"]["id"]), comment):
                closed.append(issue)
        done = ("closed " + ", ".join(f"#{issue['number']}" for issue in closed) if closed else "closed nothing") + stamped_note
        if newer:
            done += "; left open " + ", ".join(f"#{issue['number']}" for issue in newer) + " (a newer breakage)"
        if reopened:
            done += "; left open " + ", ".join(f"#{issue['number']}" for issue in reopened) + " (reopened by hand)"
        return done

    marker = episode_marker(notification, workflow_id)
    title = render_title(notification)
    body = render_body(notification, repo, marker)
    # Of two issues opened for one episode by a sweep and an event run racing,
    # the older is the one people were notified of; it stays.
    matching = [issue for issue in open_issues if marker in (issue.get("body") or "")]
    current = min(matching, key=lambda issue: issue["number"], default=None)
    # An issue a person reopened is theirs to close, on this path as on the
    # green one; it is neither superseded nor counted as a duplicate.
    stale = [
        issue for issue in open_issues if issue is not current and issue.get("state_reason") != REOPENED
    ]

    def supersede(issue, by_number):
        """Close an older issue in favour of `by_number`, stamping the green
        that ended its episode when the page in hand shows one.

        A green whose own reconciliation a following red overtook is never
        the newest reporting run again, so it never stamps its episode's
        issue itself; this is the one moment the superseding red still holds
        that green in its history. Without the stamp a later page lacking the
        green would read the old reds and the new one as one streak and rebuild
        the old episode over the correct issue.
        """
        stamp = SUPERSEDED_STAMP.format(number=by_number)
        fixer = _green_that_ended(issue, notification["page"])
        if fixer is not None:
            stamp = FIXED_BY_STAMP.format(number=fixer["run_number"], run_id=fixer["id"]) + "\n" + stamp
        api.close_issue(issue, stamp, f"Superseded by #{by_number}.")

    def own_close(issue):
        """A close the script made itself: never a decision about the breakage."""
        return (issue.get("closed_by") or {}).get("login") == OWN_CLOSER_LOGIN

    # A read that lacks a run these issues name is not a read to act on: a
    # fresh episode it suggests may be a hole in the list, and a shorter streak
    # a missing row. Checked before anything is written, for the issue this run
    # would update and for every issue it would supersede.
    gaps = incomplete_for(([current] if current is not None else []) + stale)
    if gaps:
        return leave(gaps)

    if current is None:
        # No open issue for this episode. Before opening one, look at the closed
        # ones. First for holes: a closed issue naming a run this read lacks
        # means the read is short, and a fresh episode built on it is a hole,
        # not a breakage. Then for a dismissal: an issue for this episode closed
        # after the last change to any run in the streak was closed knowing
        # everything the streak knows -- by a person, or by a merge whose
        # description named it -- and the sweep would otherwise reverse that
        # within fifteen minutes, and again after every close. A run re-run
        # after the close, or a red that landed after a "fixes" merge, moves a
        # run's timestamp past the close and is new evidence: it files.
        closed = api.issues_for_workflow(workflow_id, ISSUE_CLOSED)
        gaps = incomplete_for(closed)
        if gaps:
            return leave(gaps)
        # The mirror of the green path's re-read. A closed issue whose stamp
        # names a run this page shows as red says that run was re-run green
        # after the page was cut; ask GitHub before filing a breakage on it.
        streak_ids = {run["run_number"]: run["id"] for run in notification["streak"]}
        for issue in closed:
            for number, _ in _FIXED_BY.findall(issue.get("body") or ""):
                if int(number) in streak_ids:
                    fresh = api.run(streak_ids[int(number)])
                    if fresh is not None and fresh.get("conclusion") not in FAILING_CONCLUSIONS:
                        message = (
                            f"run {number} reads red on this page but #{issue['number']} names it as the fix "
                            f"and it is {fresh.get('conclusion')} now; writing nothing until a fuller read"
                        )
                        warn(message)
                        return message
        latest_change = max(run["updated_at"] for run in notification["streak"])
        dismissed = [
            issue
            for issue in closed
            if marker in (issue.get("body") or "")
            and SUPERSEDED_PREFIX not in (issue.get("body") or "")
            and not own_close(issue)
            and (issue.get("closed_at") or "") > latest_change
        ]
        if dismissed:
            done = f"#{dismissed[0]['number']} was closed after the last of these runs changed; leaving it"
            for issue in stale:
                supersede(issue, dismissed[0]["number"])
                done += f", superseded #{issue['number']}"
            return done
        # A closed issue of an older episode that carries no stamp -- closed by
        # a person or a merge, with the green that then ended its episode
        # overtaken by this red, so no green path ever stamped it -- gets its
        # green from the page in hand now, as a superseded open issue does.
        # Left unstamped, a later page lacking that green would read its reds
        # and this one as one streak and rebuild its episode over this one.
        for issue in closed:
            issue_body = issue.get("body") or ""
            if marker in issue_body or _FIXED_BY.search(issue_body) or SUPERSEDED_PREFIX in issue_body:
                continue
            fixer = _green_that_ended(issue, notification["page"])
            if fixer is not None:
                api.stamp(issue, FIXED_BY_STAMP.format(number=fixer["run_number"], run_id=fixer["id"]))
        api.ensure_label()
        current = api.create_issue(title, body)
        done = f"opened #{current['number']}"
    else:
        # Rewrite the body only when the set of rows changes, and comment only
        # when a row is added. A body that already lists this run was written
        # by the notify run that saw it first, and that run commented then; the
        # scheduled sweep and a redelivered event both land here, and either
        # commenting again would add a "Still failing" every fifteen minutes
        # for as long as main stays red. A row that disappears -- a run deleted
        # from GitHub -- rewrites the table but is not a commit landing, so it
        # draws no comment. Comparing rows rather than text also leaves a note
        # someone wrote into the body alone until there is news to rewrite it
        # with. A hand-edited title is restored on its own, body untouched and
        # without a comment: nothing is news.
        listed = listed_rows(current)
        streak_rows = {run["run_number"] for run in notification["streak"]}
        if listed != streak_rows:
            # Re-read before the one write that replaces a body: the list this
            # issue came from is older than this history read, and a green
            # may have closed and stamped it in between.
            fresh = api.issue(current["number"]) or current
            if fresh.get("state") == ISSUE_CLOSED:
                return f"#{current['number']} was closed since the list was read; writing nothing"
            # Comment first, for the reason `close_issue` gives: a comment that
            # fails after the rewrite would never be retried, since the next
            # reconciliation would find the rows already listed.
            if comment and streak_rows - listed:
                api.comment(current["number"], comment)
            api.update_issue(current["number"], title=title, body=body)
            done = f"updated #{current['number']}"
        elif current.get("title") != title:
            api.update_issue(current["number"], title=title)
            done = f"retitled #{current['number']}"
        else:
            done = f"#{current['number']} already says so"

    # Another issue for this workflow is still open: an older breakage whose
    # recovery never got recorded, or a newer duplicate of this episode from a
    # sweep and an event run racing. Point it at the current one and close it
    # rather than leaving two issues claiming main is broken.
    for issue in stale:
        supersede(issue, current["number"])
        done += f", superseded #{issue['number']}"
    return done


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--run-id", type=int, help="a completed workflow run; its workflow is reconciled")
    what.add_argument(
        "--sweep",
        action="store_true",
        help="reconcile every watched workflow against its newest completed push run that said anything",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "gke-labs/kube-agents"),
        help="owner/name (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument("--branch", default="main", help="the branch whose health is being reported")
    parser.add_argument("--dry-run", action="store_true", help="print the issue instead of writing it")
    return parser.parse_args(argv)


def report(api, workflow_id, runs, repo, dry_run):
    """Bring one workflow's issues into line with its newest run that said anything.

    `runs` is the workflow's completed push history on the branch. The run that
    caused this to be called, if any, is deliberately not an argument: it may
    have been overtaken, and the newest reporting run is the state of main
    whichever run raised the alarm.
    """
    current = newest_reporting_run(runs)
    if current is None:
        log(f"No completed push run of workflow {workflow_id} says anything about main; nothing to reconcile")
        return

    notification = decide(current, reporting_history(runs, current))
    notification["window"] = history_window(runs)
    notification["page"] = runs

    if dry_run:
        log(f"--dry-run: {current['name']} run {current['run_number']} is {notification['kind']}")
        if notification["kind"] != "green":
            marker = episode_marker(notification, workflow_id)
            log(f"\n# {render_title(notification)}\n\n{render_body(notification, repo, marker)}")
        comment = render_comment(notification, repo)
        if comment:
            log(f"\n--- comment, if an issue is open ---\n{comment}")
        return

    log(f"{current['name']}: {reconcile(api, notification, repo, workflow_id)}")


def _latest_run_time(runs):
    """When a workflow last ran: its newest run's creation time, or nothing."""
    return runs[0]["created_at"] if runs else ""


def sweep(api, repo, branch, dry_run):
    """Reconcile every watched workflow with no run to start from.

    Returns the exit status. A watched name that no workflow carries is the
    rename the workflow's header warns about -- `workflow_run` matches on
    `name:`, so the event path has silently stopped firing for it -- and the
    sweep goes red to say so, after reconciling the workflows it could find.

    The workflow list keeps entries for files that no longer exist, listed as
    `active` with their history frozen, so a file renamed under the same
    `name:` leaves two workflows carrying it. The one whose file is in the
    checkout is the workflow, and when none is (a run from outside the
    checkout) the one that ran most recently; the other would otherwise be
    reconciled against a history that can never change, and a red at the end
    of it would be filed forever. A ghost that is the only carrier of a name
    still counts as found: nothing distinguishes it from a workflow that has
    not run in a while.
    """
    carriers = {}
    try:
        workflows = api.workflows()
    except Exception as error:  # noqa: BLE001 - an unreadable list, whatever the shape, is handled by kind
        # The one read every workflow depends on. An API incident that outlasts
        # the client's retries, or a response of the wrong shape, must not red
        # the schedule every fifteen minutes; the next sweep retries. A request
        # GitHub refused outright is ours to fix, and does.
        if _is_our_fault(error):
            log(f"GitHub refused the workflow list ({error}); that is a fault in this repository")
            return 1
        warn(f"The workflow list could not be read ({type(error).__name__}: {error}); leaving this sweep to the next one")
        return 0
    for workflow in workflows:
        if workflow["name"] in WATCHED_WORKFLOWS:
            carriers.setdefault(workflow["name"], []).append(workflow)

    missing = []
    failed = []
    broken = []
    for name in WATCHED_WORKFLOWS:
        if not carriers.get(name):
            missing.append(name)
            continue
        try:
            candidates = [(workflow, api.history(workflow["id"], branch)) for workflow in carriers[name]]
            if any(runs is None for _, runs in candidates):
                # With a carrier unread, "which ran most recently" cannot be
                # answered, and a ghost must not win by default.
                warn(f"{name}: a history read came back short; leaving it to the next sweep")
                continue
            if len(candidates) > 1:
                # The file in the checkout is the live workflow; a ghost's path
                # is not there. Recency decides only among files that are, or
                # when none is (a run from outside the checkout), since a live
                # file that has not run yet, or whose page is stale, would
                # otherwise lose to a ghost's frozen history.
                on_disk = [pair for pair in candidates if (REPO_ROOT / pair[0]["path"]).is_file()]
                pool = on_disk or candidates
                pool.sort(key=lambda pair: _latest_run_time(pair[1]), reverse=True)
                candidates = pool + [pair for pair in candidates if pair not in pool]
                others = ", ".join(f"{w['id']} ({w['path']})" for w, _ in candidates[1:])
                log(
                    f"{name} is carried by {len(candidates)} workflows; reconciling {candidates[0][0]['id']}, "
                    f"{'the file in the checkout' if on_disk else 'which ran most recently'}, and not {others}"
                )
            workflow, runs = candidates[0]
            report(api, workflow["id"], runs, repo, dry_run)
        except Exception as error:  # noqa: BLE001 - one workflow's failure, read or write, must not stop the rest
            log(f"{name}: {type(error).__name__}: {error}")
            (broken if _is_our_fault(error) else failed).append(name)

    if missing:
        log(f"No workflow is named {', '.join(missing)}: renamed or removed, so nothing reports on it")
    if failed:
        # Logged and annotated, not a red: an API incident that outlasts the
        # client's retries would otherwise red the schedule every fifteen
        # minutes and mail the cron line's last editor each time, for a fault
        # nobody here can fix. The next sweep retries. A missing name, or a
        # request GitHub refused outright, is a red because it is ours to fix.
        warn(f"Reconciling {', '.join(failed)} failed; the rest were reconciled, and the next sweep retries")
    if broken:
        log(f"GitHub refused a request while reconciling {', '.join(broken)}; that is a fault in this repository")
    return 1 if missing or broken else 0


def _is_our_fault(error):
    """A 4xx other than a rate limit: a permission or a request GitHub rejects,
    which retrying will not change and a person here has to fix."""
    return (
        isinstance(error, urllib.error.HTTPError)
        and error.code < HTTP_SERVER_ERROR_FLOOR
        and error.code != HTTP_TOO_MANY_REQUESTS
        and not _rate_limited(error)
    )


def main(argv=None):
    args = parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("GITHUB_TOKEN (or GH_TOKEN) is not set")
        return 1

    api = GitHubAPI(args.repo, token)

    if args.sweep:
        return sweep(api, args.repo, args.branch, args.dry_run)

    current = api.run(args.run_id)
    if current is None:
        log(f"Run {args.run_id} no longer exists; nothing to reconcile from it")
        return 0

    # The workflow's `if:` has already checked these, against the event payload.
    # Reading them back off the run is what makes a hand-run of this script on
    # the wrong run id harmless rather than an issue about a pull request.
    if current["head_branch"] != args.branch or current["event"] != "push":
        log(f"Run {args.run_id} is {current['event']} on {current['head_branch']}, not push on {args.branch}")
        return 0
    if current["name"] not in WATCHED_WORKFLOWS:
        # The trigger list keeps this from happening in the workflow; a hand-run
        # on, say, a `Prettier Check` run would file for a workflow the sweep
        # never reconciles, and the header says why it is not watched.
        log(f"Run {args.run_id} belongs to {current['name']}, which is not watched; nothing to do")
        return 0

    workflow_id = current["workflow_id"]
    runs = api.history(workflow_id, args.branch)
    if runs is None:
        warn("The history read came back short; leaving this to the next run or the sweep")
        return 0
    # The list is read moments after the run completed and lists can lag the
    # run they are about, or carry it with the conclusion a re-run has since
    # changed; the run that woke this was read directly and is known to have
    # completed, so its copy replaces the list's. Order does not matter here:
    # everything downstream picks by run number or sorts for itself.
    runs = [current] + [run for run in runs if run["id"] != current["id"]]

    # Notify runs are queued in the order the runs they watch *finish*, which is
    # not the order those runs started, and a burst of merges drops some of them
    # outright. So this run's own conclusion is not what gets filed: whatever
    # the newest completed run of the workflow says is, and this run is only the
    # reason to look. Out of order, that keeps a red handled after the green
    # that fixed it from opening an issue against a green main; in a burst, it
    # lets whichever notify run survives file for the one that was cancelled.
    newest = newest_reporting_run(runs)
    if newest is not None and newest["id"] != current["id"]:
        log(
            f"Run {current['run_number']} ({current['conclusion']}) woke this; "
            f"run {newest['run_number']} is the newest completed run of {current['name']} "
            f"that says anything, so that is the one reconciled"
        )

    report(api, workflow_id, runs, args.repo, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
