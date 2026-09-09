#!/usr/bin/env python3
"""Keep a pull request with unresolved review threads out of Tide's merge pool.

`main` requires every review conversation resolved before a merge, and Tide
does not read that rule. A pull request carrying `lgtm` and `approved` is in
its pool whatever its threads say; Tide picks the lowest-numbered one, GitHub
refuses the merge ("Repository rule violations found"), and Tide retries the
same pull request every ~85 seconds -- ahead of every other pull request in
the pool, which wait behind it. #608 and #1197 held the whole queue that way
for an hour on 2026-09-05 until an admin `/hold`ed them by hand; #1122 sat
5h46m and 231 attempts the same way (#1202).

Tide's query for this repository excludes four labels. `do-not-merge` -- the
bare one, not `do-not-merge/hold` -- is the one no Prow plugin writes, so this
script owns it. Each sweep lists the open pull requests carrying both pool
labels and puts `do-not-merge` on any whose review threads are not all
resolved, with one comment saying why; and takes it off any pull request whose
threads are now all resolved -- but only when the label's most recent
application was its own, so a label a person put there by hand stays until
that person removes it. Resolving the threads is the only exit: `/hold cancel`
does not touch this label, which is the point.

The pool is listed from the pulls endpoint, not search: the workflow's fast
path runs the moment `lgtm` lands, and the search index that would list the
label is eventually consistent, so a search-based sweep could miss the pull
request the event was about. Thread state is GraphQL because REST has no
notion of a resolved thread. Who applied the label is read from the issue
timeline rather than remembered anywhere: a sweep that crashed after labelling
leaves nothing behind for the next one to need.
"""

import argparse
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from github_api import GitHubAPI, log  # noqa: E402

#: Excluded by Tide's query for this repository (`prow/oss/config.yaml` in oss-test-infra) and
#: written by no Prow plugin, which is what lets this script own it outright.
LABEL = "do-not-merge"
LABEL_COLOR = "b60205"
LABEL_DESCRIPTION = "Review threads are unresolved; removed automatically once they all are"
#: Tide's pool wants both. A pull request without them is not one Tide will try to merge.
POOL_LABELS = frozenset({"lgtm", "approved"})
#: The login a GITHUB_TOKEN acts as; a `labeled` event by anyone else is a person's decision.
BOT_LOGIN = "github-actions[bot]"
#: Opens the explanatory comment. A pull request that already carries one gets no second: a
#: label removed by hand while threads stay open is re-applied every sweep, and the comment
#: would otherwise be re-posted with it.
COMMENT_MARKER = "<!-- hold-unresolved-threads -->"
#: GraphQL's per-page ceiling; the thread query pages past it.
PAGE = 100
USER_AGENT = "kube-agents-hold-unresolved-threads"
TIMELINE_LABELED = "labeled"
#: Interval `.github/workflows/hold-unresolved-threads.yml` sweeps on, quoted in the comment.
SWEEP_MINUTES = 5
ACTION_ADD = "add"
ACTION_REMOVE = "remove"

THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: %d, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved }
      }
    }
  }
}
""" % PAGE

COMMENT = f"""{COMMENT_MARKER}
Tide would try to merge this pull request and fail: **{{count}} review thread{{plural}} unresolved**, \
and `main` requires every conversation resolved before a merge. Tide retries a failed merge every \
~85 seconds ahead of every other pull request in the pool, so the `{LABEL}` label keeps this one out \
of the pool until the threads are resolved.

Reply to each thread naming what changed, then resolve it \
([`docs/pull-request-workflow.md`, "Resolving conversations"](https://github.com/{{repo}}/blob/main/docs/pull-request-workflow.md#resolving-conversations)). \
The label comes off by itself at the next sweep (every {SWEEP_MINUTES} minutes) after the last one; `/hold cancel` does not remove it."""


def open_pulls(api):
    """Every open pull request: number, draft flag, label names. Consistent, unlike search."""
    pulls = {}
    for pull in api.get_all(f"/repos/{api.repo}/pulls?state=open"):
        pulls[pull["number"]] = {
            "number": pull["number"],
            "draft": bool(pull.get("draft")),
            "labels": {label["name"] for label in pull.get("labels") or []},
        }
    return pulls


def concerns_us(pull):
    """In the pool, or carrying our label: the two populations a sweep reconciles."""
    return (POOL_LABELS <= pull["labels"] and not pull["draft"]) or LABEL in pull["labels"]


def unresolved_threads(api, number):
    """How many review threads on the pull request are not resolved, every page counted."""
    owner, name = api.repo.split("/", 1)
    count = 0
    after = None
    while True:
        threads = api.graphql(
            THREADS_QUERY, {"owner": owner, "name": name, "number": number, "after": after}
        )["repository"]["pullRequest"]["reviewThreads"]
        count += sum(1 for thread in threads["nodes"] if not thread["isResolved"])
        if not threads["pageInfo"]["hasNextPage"]:
            return count
        after = threads["pageInfo"]["endCursor"]


def label_is_ours(api, number):
    """Whether the most recent application of LABEL on the pull request was this script's.

    The timeline is the record: the last `labeled` event for the label names
    who did it. A person's label is theirs to remove; a label with no event at
    all (an API gap) is treated the same way, because removing a label nobody
    is known to have applied is the wrong side to err on.
    """
    actor = None
    for event in api.get_all(f"/repos/{api.repo}/issues/{number}/timeline"):
        if event.get("event") == TIMELINE_LABELED and (event.get("label") or {}).get("name") == LABEL:
            actor = (event.get("actor") or {}).get("login")
    return actor == BOT_LOGIN


def already_explained(api, number):
    """Whether a comment carrying COMMENT_MARKER is already on the pull request."""
    return any(
        (comment.get("body") or "").startswith(COMMENT_MARKER)
        for comment in api.get_all(f"/repos/{api.repo}/issues/{number}/comments")
    )


def decide(pull, unresolved):
    """(ACTION_ADD | ACTION_REMOVE | None, reason) for one pull request, from what a sweep read.

    Pure so the tests can drive it. The ownership check for ACTION_REMOVE is
    the caller's: it costs a timeline read this function should not force on
    every pull request.
    """
    labelled = LABEL in pull["labels"]
    in_pool = POOL_LABELS <= pull["labels"] and not pull["draft"]
    if unresolved and in_pool and not labelled:
        return ACTION_ADD, f"{unresolved} unresolved thread(s) and in the pool"
    if unresolved and labelled:
        return None, f"{unresolved} unresolved thread(s); already labelled"
    if unresolved:
        return None, f"{unresolved} unresolved thread(s) but not in the pool; nothing to keep out"
    if labelled:
        return ACTION_REMOVE, "every thread resolved"
    return None, "every thread resolved; not labelled"


def ensure_label(api, dry_run=False):
    """Create LABEL if the repository lacks it. 422 is 'already exists'."""
    if dry_run:
        return
    api.post(
        f"/repos/{api.repo}/labels",
        {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION},
        tolerate=(422,),
    )


def sweep(api, dry_run=False):
    """Reconcile every pull request in the pool or carrying LABEL. Returns (log lines, failures)."""
    lines = []
    failures = 0
    pulls = {number: pull for number, pull in open_pulls(api).items() if concerns_us(pull)}
    ensure_label(api, dry_run=dry_run)
    for number in sorted(pulls):
        pull = pulls[number]
        try:
            unresolved = unresolved_threads(api, number)
            action, reason = decide(pull, unresolved)
            if action == ACTION_REMOVE and not label_is_ours(api, number):
                action, reason = None, "every thread resolved, but the label was applied by a person; leaving it"
            if action == ACTION_ADD and not dry_run:
                api.post(f"/repos/{api.repo}/issues/{number}/labels", {"labels": [LABEL]})
                if already_explained(api, number):
                    reason += "; the comment is already there"
                else:
                    api.post(
                        f"/repos/{api.repo}/issues/{number}/comments",
                        {"body": COMMENT.format(count=unresolved, plural="s are" if unresolved != 1 else " is", repo=api.repo)},
                    )
            elif action == ACTION_REMOVE and not dry_run:
                api.delete(f"/repos/{api.repo}/issues/{number}/labels/{LABEL}", tolerate=(404,))
            verb = {ACTION_ADD: "labelled", ACTION_REMOVE: "unlabelled", None: "left alone"}[action]
            if action and dry_run:
                verb = f"would be {verb}"
            line = f"#{number}: {verb}: {reason}"
        except Exception as error:  # one pull request must not stop the sweep
            failures += 1
            line = f"#{number}: failed: {error}"
        log(line)
        lines.append(line)
    return lines, failures


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name")
    parser.add_argument("--dry-run", action="store_true", help="log what would change, change nothing")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not args.repo or not token:
        print("GITHUB_REPOSITORY (or --repo) and GITHUB_TOKEN are required", file=sys.stderr)
        return 2
    api = GitHubAPI(args.repo, token, user_agent=USER_AGENT)
    _, failures = sweep(api, dry_run=args.dry_run)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
