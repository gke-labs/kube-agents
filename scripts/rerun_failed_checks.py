#!/usr/bin/env python3
"""Re-run the failed GitHub Actions checks on a pull request, for `/retest`.

GitHub lets Write and above re-run a workflow; Triage cannot, and most of this
repository's collaborators hold Triage. Prow's `/retest` re-runs only Prow jobs.
So a GitHub Actions check that flaked on someone else's pull request stayed red
until an admin clicked re-run, and `docs/pull-request-workflow.md` ("Re-running
a check that failed") gave a `gh run rerun` command most readers could not run.

`.github/workflows/retest-actions.yml` runs this on a `/retest` comment from an
owner, member or collaborator, with a token that can re-run workflows. The same
comment reaches Prow, which re-runs its own failed jobs, so one word covers
both kinds of check.

What is re-run is what Tide sees red, read the way Tide reads it. Tide takes
the check runs at the head commit -- the latest attempt of each check suite,
which is what GitHub's commit rollup lists -- and keeps the best state per
check name: success (`success`, `neutral`, `skipped`) over pending (not
completed) over failure (any other completed conclusion). A name whose best
state is failure blocks the merge, a pending one holds it, and only a success
clears it. So the script lists the same check runs, finds the names whose
best state is failure, takes the newest red check run of each, and re-runs
the GitHub Actions workflow run that produced it: through its
failed jobs when the run concluded `failure`, whole otherwise (a `cancelled`
or `timed_out` run has no failed job for that endpoint to find). A red check
run from another app (the review bot's `AI Review`) is not a workflow run and
is named in the log and left alone.

Reading check runs rather than workflow runs is what keeps the command from
spending runners on nothing. A workflow run cancelled while still queued -- its
concurrency group displaced it -- never created a check run, and a run an
`edited` event superseded left a check run whose name a newer success covers;
both read `cancelled` on the runs list and neither blocks anything.

A re-run executes the original run again, with the privileges of the actor
who first triggered it, so the person typing the command gains no ability to
run pull-request code with a write token; the workflow itself checks out the
default branch only.

The ceiling (`RERUN_ATTEMPT_CEILING`) is what keeps this a flake retry rather
than a way to grind a real break to green: a run already on that attempt is
left alone and the person is told so. `scripts/notify_flaky_check.py` records
each re-run that goes green as a `ci:flaky` occurrence; one made here lists
the Actions bot as who re-ran, and the `/retest` comment on the pull request
names the person.

Run:  python3 scripts/rerun_failed_checks.py --pr 3245 --dry-run
Test: cd scripts && python3 -m unittest test_rerun_failed_checks
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import urllib.error

from github_api import GitHubAPI, is_refusal, log

DEFAULT_REPO = "gke-labs/kube-agents"
USER_AGENT = "kube-agents-rerun-failed-checks"
# The pull request state the command acts on; a closed or merged one is declined.
PULL_REQUEST_OPEN = "open"

# How Tide reads a check run (prow `tide`, `checkRunToContext`): not completed
# is pending; these conclusions are success; every other completed conclusion
# (`failure`, `cancelled`, `timed_out`, `action_required`, `stale`) is failure.
COMPLETED_STATUS = "completed"
SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
# The per-name order Tide keeps (`isStateBetter`): one success clears the name,
# one pending holds it, failure only when every check run of the name failed.
STATE_SUCCESS, STATE_PENDING, STATE_FAILURE = "success", "pending", "failure"

# Which check runs the commit endpoint lists: the latest attempt per check
# suite, as the rollup Tide reads does (`list_check_runs` says why not `all`).
CHECK_RUNS_FILTER = "latest"

# Check runs GitHub Actions itself posts carry this app slug. Any other app's
# red check run is not a workflow run and this command cannot re-run it.
ACTIONS_APP_SLUG = "github-actions"

# The events whose workflow runs belong to a pull request. The runs list is
# keyed by commit, not by pull request: at a head commit that is also a
# commit on `main` or a release line it lists that line's `push`,
# `workflow_run` and `status` runs too, and a red one of those would be
# re-run with its own credentials. So a run counts only when its event is
# one of these and its head branch and repository are the pull request's.
PULL_REQUEST_EVENTS = frozenset({"pull_request", "pull_request_target"})

# What a non-completed run's `status` means to the person waiting on it.
# `queued`, `in_progress` and `requested` finish on their own;
# `action_required` waits for someone with Write access to approve the run
# (a first-time contributor's fork); `waiting` and `pending` sit on an
# environment's protection rules or a deployment review.
STATUS_AWAITING_APPROVAL = "action_required"
STATUSES_AWAITING_ENVIRONMENT = frozenset({"waiting", "pending"})

# A run that concluded `failure` has failed jobs to re-run; any other red
# conclusion has none, so those re-run whole.
FAILED_CONCLUSION = "failure"
RERUN_FAILED_JOBS = "rerun-failed-jobs"
RERUN_WHOLE = "rerun"

# A run already on this attempt is not re-run again by the command. The doc
# calls a flake "worth one re-run"; the second is for a run that a cancelled
# attempt (a runner lost, a superseding event) put in the way of that one.
# Past it, a person with Write reads the log and decides.
RERUN_ATTEMPT_CEILING = 3

# Reactions on the `/retest` comment: 👀 when something was re-run, 😕 when the
# command found nothing to do. Without the second, a declined command looks
# identical to the workflow never firing.
REACTION_ACKNOWLEDGED = "eyes"
REACTION_DECLINED = "confused"

# A declined command is written where the person who typed it will see it: a
# warning annotation on the workflow run (the `::warning::` command goes to
# stdout) and the job's step summary, when Actions provides one.
WORKFLOW_WARNING_PREFIX = "::warning::"
STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"
# How the step summary is written and a refusal body is read.
TEXT_ENCODING = "utf-8"
DECODE_ERRORS = "replace"


@dataclasses.dataclass
class Rerun:
    run: dict
    action: str
    check_names: list


@dataclasses.dataclass
class Skipped:
    name: str
    reason: str


@dataclasses.dataclass
class Plan:
    rerun: list  # Rerun, one per workflow run behind a red check name
    skipped: list  # Skipped, one per red check name left alone, with why
    pending: int  # check names whose best state is pending


def list_runs(api, repo: str, head_sha: str) -> list:
    """Every workflow run at `head_sha`, each as its latest attempt."""
    return api.get_object_pages(f"/repos/{repo}/actions/runs", "workflow_runs", {"head_sha": head_sha})


def list_check_runs(api, repo: str, head_sha: str) -> list:
    """The check runs at `head_sha` as Tide sees them: the latest attempt of
    each check suite. `latest` is the endpoint's default, named here because
    `all` would also return the attempts a re-run replaced, which the commit
    rollup Tide reads does not list -- so a check that passed once and failed
    on a later re-run would read green here and red to Tide. A run a newer
    event superseded is a check suite of its own and is listed either way."""
    return api.get_object_pages(f"/repos/{repo}/commits/{head_sha}/check-runs", "check_runs", {"filter": CHECK_RUNS_FILTER})


def check_state(check_run: dict) -> str:
    if check_run.get("status") != COMPLETED_STATUS:
        return STATE_PENDING
    if check_run.get("conclusion") in SUCCESS_CONCLUSIONS:
        return STATE_SUCCESS
    return STATE_FAILURE


def best_state(states) -> str:
    states = set(states)
    if STATE_SUCCESS in states:
        return STATE_SUCCESS
    if STATE_PENDING in states:
        return STATE_PENDING
    return STATE_FAILURE


def red_check_runs(check_runs) -> tuple:
    """The newest check run of each name whose best state is failure, by name,
    and how many names are pending."""
    by_name = {}
    for check_run in check_runs:
        by_name.setdefault(check_run["name"], []).append(check_run)
    red = {}
    pending = 0
    for name, entries in by_name.items():
        state = best_state(check_state(entry) for entry in entries)
        if state == STATE_PENDING:
            pending += 1
        elif state == STATE_FAILURE:
            red[name] = max(entries, key=lambda entry: entry["id"])
    return red, pending


def belongs_to(run: dict, head: dict) -> bool:
    """Whether a workflow run is the pull request's: a pull-request event at its head branch and repository."""
    repository = (run.get("head_repository") or {}).get("full_name")
    return (
        run.get("event") in PULL_REQUEST_EVENTS
        and run.get("head_branch") == head.get("ref")
        and repository == ((head.get("repo") or {}).get("full_name"))
    )


def waiting_reason(run: dict) -> str:
    """Why a non-completed run is not re-run, in terms of what the person can do about it."""
    status = run.get("status")
    if status == STATUS_AWAITING_APPROVAL:
        return f"its workflow run {run['id']} awaits approval from someone with Write access"
    if status in STATUSES_AWAITING_ENVIRONMENT:
        return f"its workflow run {run['id']} is waiting on an environment or a deployment review"
    return f"its workflow run {run['id']} is still running"


def plan_reruns(check_runs, runs, head: dict) -> Plan:
    """Which workflow runs to re-run and how, and which red names to leave alone and why.

    `head` is the pull request's `head` object (`ref`, `repo.full_name`); a
    run at the commit that is not the pull request's is never re-run.
    """
    red, pending = red_check_runs(check_runs)
    by_suite = {run.get("check_suite_id"): run for run in runs}
    plan = Plan(rerun=[], skipped=[], pending=pending)
    by_run = {}
    for name in sorted(red):
        check_run = red[name]
        app = (check_run.get("app") or {}).get("slug")
        if app != ACTIONS_APP_SLUG:
            plan.skipped.append(Skipped(name, f"posted by {app or 'an unknown app'}, not a workflow run"))
            continue
        run = by_suite.get((check_run.get("check_suite") or {}).get("id"))
        if run is None:
            plan.skipped.append(Skipped(name, "no workflow run at the head commit carries it"))
            continue
        if not belongs_to(run, head):
            plan.skipped.append(
                Skipped(name, f"its workflow run {run['id']} is a {run.get('event')} run on {run.get('head_branch')}, not this pull request's")
            )
            continue
        if run.get("status") != COMPLETED_STATUS:
            plan.skipped.append(Skipped(name, waiting_reason(run)))
            continue
        attempt = run.get("run_attempt") or 1
        if attempt >= RERUN_ATTEMPT_CEILING:
            plan.skipped.append(Skipped(name, f"run {run['id']} is already on attempt {attempt}; a person with Write access decides"))
            continue
        if run["id"] not in by_run:
            action = RERUN_FAILED_JOBS if run.get("conclusion") == FAILED_CONCLUSION else RERUN_WHOLE
            by_run[run["id"]] = Rerun(run, action, [])
            plan.rerun.append(by_run[run["id"]])
        by_run[run["id"]].check_names.append(name)
    return plan


def describe(entry: Rerun) -> str:
    run = entry.run
    return f"{run.get('name')} (run {run.get('id')}, attempt {run.get('run_attempt')}, {run.get('conclusion')}): {', '.join(entry.check_names)}"


def refusal_message(error: urllib.error.HTTPError) -> str:
    """The `message` GitHub puts in a refusal's body, or the raw body, or the status reason."""
    try:
        raw = error.read().decode(TEXT_ENCODING, errors=DECODE_ERRORS)
    except (OSError, ValueError, AttributeError):
        raw = ""
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        parsed = {}
    return (parsed.get("message") if isinstance(parsed, dict) else None) or raw.strip() or str(error.reason)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pr", type=int, required=True, help="pull request number")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO),
        help="owner/name (default: $GITHUB_REPOSITORY)",
    )
    parser.add_argument(
        "--react-to",
        type=int,
        metavar="COMMENT_ID",
        help=f"the /retest comment: reacts {REACTION_ACKNOWLEDGED} when something is re-run and {REACTION_DECLINED} when nothing is",
    )
    parser.add_argument("--dry-run", action="store_true", help="print what would be re-run")
    return parser.parse_args(argv)


def react(api, args, content):
    if not args.react_to:
        return
    if args.dry_run:
        log(f"--dry-run: would react {content} to comment {args.react_to}")
        return
    api.post(f"/repos/{args.repo}/issues/comments/{args.react_to}/reactions", {"content": content})


def summarize(lines):
    summary_path = os.environ.get(STEP_SUMMARY_ENV)
    if summary_path:
        with open(summary_path, "a", encoding=TEXT_ENCODING) as handle:
            handle.write("\n".join(lines) + "\n")


def warn(lines):
    for line in lines:
        print(f"{WORKFLOW_WARNING_PREFIX}{line}", flush=True)


def decline(api, args, reason, details=()):
    """Record that nothing was re-run, and why, where the person will see it."""
    message = f"Not re-running anything: {reason}"
    log(message)
    warn([message, *details])
    summarize([message, *details])
    react(api, args, REACTION_DECLINED)


def main(argv=None):
    args = parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        log("GITHUB_TOKEN (or GH_TOKEN) is not set")
        return 1

    api = GitHubAPI(args.repo, token, user_agent=USER_AGENT)

    pull_request = api.get(f"/repos/{args.repo}/pulls/{args.pr}")
    head_sha = pull_request["head"]["sha"]
    log(f"#{args.pr} at {head_sha[:12]}: {pull_request.get('title')}")
    if pull_request.get("state") != PULL_REQUEST_OPEN:
        decline(api, args, f"#{args.pr} is {pull_request.get('state')}")
        return 0

    plan = plan_reruns(list_check_runs(api, args.repo, head_sha), list_runs(api, args.repo, head_sha), pull_request["head"])
    skipped_lines = [f"Left alone: {entry.name}: {entry.reason}" for entry in plan.skipped]
    if not plan.rerun:
        reason = "no red GitHub Actions check at the head commit"
        if plan.skipped:
            reason = "no red GitHub Actions check at the head commit that this command can re-run"
        if plan.pending:
            reason += f"; {plan.pending} still running"
        decline(api, args, reason, skipped_lines)
        return 0

    done = []
    refused = []
    for entry in plan.rerun:
        path = f"/repos/{args.repo}/actions/runs/{entry.run['id']}/{entry.action}"
        if args.dry_run:
            log(f"--dry-run: would re-run {describe(entry)} via {entry.action}")
            done.append(entry)
            continue
        try:
            api.post(path)
        except urllib.error.HTTPError as error:
            # GitHub refuses a re-run it cannot make -- a run older than thirty
            # days, one already re-running -- and refuses every re-run when the
            # token cannot re-run at all. The message tells them apart, so it
            # is logged; the rest still go; and a run where nothing could be
            # re-run exits red below, so a broken command cannot pass for a
            # quiet one. Anything that is not a refusal is the API failing,
            # and raises.
            if not is_refusal(error):
                raise
            message = refusal_message(error)
            log(f"{describe(entry)}: GitHub refused the re-run ({error.code}: {message})")
            refused.append((entry, message))
            continue
        log(f"Re-ran {describe(entry)} via {entry.action}")
        done.append(entry)

    lines = [f"Re-ran {describe(entry)} ({entry.action})" for entry in done]
    noted = [f"GitHub refused to re-run {describe(entry)}: {message}" for entry, message in refused] + skipped_lines
    warn(noted)
    summarize(lines + noted)
    react(api, args, REACTION_ACKNOWLEDGED if done else REACTION_DECLINED)
    if refused and not done:
        log("Every re-run was refused; failing the run so the refusal is seen")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
