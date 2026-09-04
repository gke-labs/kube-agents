#!/usr/bin/env python3
"""Keep a green `pull-kube-agents-smoke-test` status valid when `main` moves.

Tide credits a presubmit only against the base SHA it ran on. Crier writes that
SHA into the commit status as a `BaseSHA:<sha>` suffix, and Tide reads it back
(`prowJobsFromContexts`) so a result outlives the ProwJob object -- as long as
the SHA still names the head of `main`. Every merge to `main` therefore turns
every other pull request's green smoke run stale, and Tide re-runs the 1.5-3.5h
job for a pull request whose head has not changed (#1179, #1202).

This re-pins the suffix. On every push to `main` it sweeps the open pull
requests and re-posts each green smoke status with `BaseSHA:` set to the new
head; when a green arrives after `main` has already moved past the base it
ran on, the `status` event does the same for that one commit. The description
says what happened, so a reader is not told the run was against a base it was
not. Tide's newer form of this -- a `[prow:skip-retest]` sentinel that
`/override-sticky` writes -- is upstream since July 2026 but not in the Prow
build this repository merges through (its plugin help does not list
`/override-sticky`); when it is, append the sentinel here and stop pinning.

It is best-effort against Tide's own clock. Tide syncs about once a minute,
and a sync that sees the stale statuses before the sweep has re-pinned them
starts the retest -- as a batch, when two or more pull requests qualify --
and crier's `pending` is then the newer word, which this leaves alone. So the
sweep is kept short (no dependencies, one read per open pull request, and two
more -- `main` again, the status again -- only for the ones it writes), takes
the pull requests Tide is actually waiting on first, and is still a race that
is sometimes lost. Two merges seconds apart start two sweeps with no ordering
between them, so a pin is always made to the head of `main` as read just
before the write, never to the head the run started with.

What it will not do: touch a status that is not `success`, touch an admin
`/override`, pin a pull request that does not target `main` (Tide keys the
base SHA on the pull request's own base branch), or overwrite a newer status
on the same context. Deciding and writing are separate calls, and a throttled
call retries with a delay of seconds to a minute, so the status is read a
second time immediately before the write and the write is dropped if a newer
one has landed. What stays open is the POST itself. One pull request failing
-- a head force-pushed away mid-sweep, a call out of retries -- is logged and
the sweep goes on, because every pull request after it would otherwise wait
for the next merge; the failure still sets the exit status.
"""

import argparse
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from github_api import GitHubAPI, log  # noqa: E402

CONTEXT = "pull-kube-agents-smoke-test"
#: `contextDescriptionBaseSHADelimiter` in kubernetes-sigs/prow, pkg/config/config.go.
BASE_SHA_DELIMITER = " BaseSHA:"
#: What the override plugin writes; its statuses are an admin's decision, not ours.
OVERRIDE_PREFIX = "Overridden by"
#: Left in the human-readable part so the description does not claim a run it did not have.
PIN_NOTE = "(base pinned to main by smoke-test-sticky)"
#: GitHub rejects a longer status description; Prow's `contextDescriptionMaxLen` agrees.
MAX_DESCRIPTION = 140
SUCCESS = "success"
MAIN_BRANCH = "main"
MAIN_REF = f"heads/{MAIN_BRANCH}"
USER_AGENT = "kube-agents-smoke-test-sticky"
#: Tide's pool wants both; a pull request carrying them is the one a stale base costs hours.
POOL_LABELS = frozenset({"lgtm", "approved"})
HOLD_LABEL_PREFIX = "do-not-merge"
SHORT_SHA = 8
_SHA = re.compile(r"[0-9a-f]{40}")


def split_description(description):
    """(human-readable part, base SHA or None) of a crier-shaped description.

    Crier left-pads the suffix with U+2001 so it lines up in the GitHub UI; that
    is Unicode whitespace, which `str.split()` collapses. A previous pin note is
    dropped so re-pinning does not stack them.
    """
    text = " ".join((description or "").split())
    human, _, base_sha = text.partition(BASE_SHA_DELIMITER)
    human = " ".join(human.replace(PIN_NOTE, "").split())
    return human, (base_sha if _SHA.fullmatch(base_sha) else None)


def pinned_description(description, main_sha):
    """The same status, its base pinned to `main_sha`, within GitHub's limit.

    The note is dropped whole when it does not fit: a sliced one would not be
    recognised by the next pin and would stack.
    """
    human, _ = split_description(description)
    suffix = f"{BASE_SHA_DELIMITER}{main_sha}"
    room = MAX_DESCRIPTION - len(suffix)
    noted = f"{human} {PIN_NOTE}".strip()
    if len(noted) > room:
        noted = human[:room].rstrip()
    return noted + suffix


def skip_reason(status, main_sha):
    """Why a status is left alone, or None when it should be pinned."""
    if status is None:
        return f"no {CONTEXT} status on the commit"
    if status.get("state") != SUCCESS:
        return f"state is {status.get('state')!r}, not {SUCCESS}"
    description = status.get("description") or ""
    if description.startswith(OVERRIDE_PREFIX):
        return "an admin override, which is not ours to extend"
    _, base_sha = split_description(description)
    if base_sha == main_sha:
        return "already pinned to the head of main"
    return None


def latest_status(api, sha):
    """The most recent status for CONTEXT on `sha`, from the combined status."""
    combined = api.get(f"/repos/{api.repo}/commits/{sha}/status")
    for status in combined.get("statuses") or []:
        if status.get("context") == CONTEXT:
            return status
    return None


def open_pulls_against_main(api):
    return api.get_all(f"/repos/{api.repo}/pulls?state=open&base={MAIN_BRANCH}")


def targets_main(api, sha):
    """Whether `sha` is the head of an open pull request against main.

    Matched against the open list rather than `/commits/{sha}/pulls`: that
    endpoint returns nothing for a head that lives in a fork, which is where
    nearly every pull request here comes from.
    """
    return any(p["head"]["sha"] == sha for p in open_pulls_against_main(api))


#: Compares equal to no SHA, so `skip_reason` can be asked the questions that need no `main`.
_NO_MAIN = object()


def _resolve(main_sha):
    return main_sha() if callable(main_sha) else main_sha


def pin_head(api, sha, main_sha, status_id=None, dry_run=False, check_base=True, expected_main=None):
    """Re-pin one commit's smoke status. Returns what happened, for the log.

    `main_sha` is a SHA or a callable returning one. `expected_main` is the head
    of `main` a stale base is compared against -- a sweep reads it once for all
    its pull requests -- and defaults to `main_sha` itself. Two reads sit
    between deciding and writing: `main` again, so a sweep overlapping a newer
    one cannot pin backwards, and the status again, so a `pending` that landed
    meanwhile -- a push, `/test`, or Tide's own retest -- is not buried under a
    stale success. Neither read is made for a status that is left alone.
    """
    if check_base and not targets_main(api, sha):
        return f"{sha[:SHORT_SHA]}: not the head of an open pull request against {MAIN_BRANCH}"
    status = latest_status(api, sha)
    if status_id is not None and status is not None and str(status.get("id")) != str(status_id):
        return f"{sha[:SHORT_SHA]}: status {status_id} is no longer the latest (now {status.get('id')}); leaving it"
    reason = skip_reason(status, _NO_MAIN)
    if reason:
        return f"{sha[:SHORT_SHA]}: {reason}"
    main_now = expected_main if expected_main is not None else _resolve(main_sha)
    reason = skip_reason(status, main_now)
    if reason is None and expected_main is not None and callable(main_sha):
        main_now = main_sha()  # just before the write, not as of the start of the sweep
        reason = skip_reason(status, main_now)
    if reason:
        return f"{sha[:SHORT_SHA]}: {reason}"
    latest = latest_status(api, sha)
    if latest is None or str(latest.get("id")) != str(status.get("id")):
        newer = latest.get("id") if latest else "none"
        return f"{sha[:SHORT_SHA]}: status {status.get('id')} was superseded while deciding (now {newer}); leaving it"
    main_sha = main_now
    payload = {
        "state": SUCCESS,
        "context": CONTEXT,
        "target_url": status.get("target_url") or "",
        "description": pinned_description(status.get("description"), main_sha),
    }
    if not dry_run:
        api.post(f"/repos/{api.repo}/statuses/{sha}", payload)
    return f"{sha[:SHORT_SHA]}: pinned to {main_sha[:SHORT_SHA]}: {payload['description']}"


def in_tide_pool(pull_request):
    """Carries the labels Tide merges on and no hold: the ones a stale base costs hours."""
    labels = {label.get("name") for label in pull_request.get("labels") or []}
    return POOL_LABELS <= labels and not any(str(name).startswith(HOLD_LABEL_PREFIX) for name in labels)


def sweep(api, main_sha, dry_run=False):
    """Every open pull request's head, after a push to main; the pool first.

    Returns (outcomes, failures). One pull request's failure does not end the
    sweep -- every pull request after it would otherwise wait for the next
    merge, and a lost pin costs a 1.5-3.5h retest -- but it is counted, so the
    run can exit non-zero and be seen. Each outcome is logged as it happens,
    not after the loop: the log is the only record of the writes this makes,
    and a runner killed mid-sweep must not take the record of the ones already
    made with it.
    """
    expected = _resolve(main_sha)  # once, for the comparisons; each write re-reads it
    outcomes, failures = [], 0
    pulls = sorted(open_pulls_against_main(api), key=lambda p: not in_tide_pool(p))
    for pull_request in pulls:
        sha = pull_request["head"]["sha"]
        try:
            outcome = pin_head(api, sha, main_sha, dry_run=dry_run, check_base=False, expected_main=expected)
        except Exception as error:  # noqa: BLE001 -- one pull request must not end the sweep
            failures += 1
            outcome = f"{sha[:SHORT_SHA]}: failed, moving on: {error}"
        log(outcome)
        outcomes.append(outcome)
    return outcomes, failures


def main_head(api):
    return api.get(f"/repos/{api.repo}/git/ref/{MAIN_REF}")["object"]["sha"]


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name")
    sub = parser.add_subparsers(dest="mode", required=True)
    status = sub.add_parser("status", help="one commit, from a status event")
    status.add_argument("--sha", required=True)
    status.add_argument("--status-id", required=True, help="the event's status id; a newer one wins")
    sweep_ = sub.add_parser("sweep", help="every open pull request against main, after a push to main")
    # On the subcommands, not the parser: argparse rejects a parent option
    # given after the subcommand, and the workflow writes them after it.
    for command in (status, sweep_):
        command.add_argument("--main-sha", help="pin to this instead of the head of main as read before each write")
        command.add_argument("--dry-run", action="store_true", help="log what would be posted, post nothing")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not args.repo or not token:
        print("GITHUB_REPOSITORY (or --repo) and GITHUB_TOKEN are required", file=sys.stderr)
        return 2
    api = GitHubAPI(args.repo, token, user_agent=USER_AGENT)
    main_sha = args.main_sha or (lambda: main_head(api))
    if args.mode == "status":
        log(pin_head(api, args.sha, main_sha, status_id=args.status_id, dry_run=args.dry_run))
        return 0
    _, failures = sweep(api, main_sha, dry_run=args.dry_run)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
