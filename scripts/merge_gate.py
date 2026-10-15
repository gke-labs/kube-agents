#!/usr/bin/env python3
"""The merge queue's two gate checks.

Part of the Tide-to-merge-queue migration (#1363). Tide decides two things
that `main`'s required GitHub checks do not: the smoke test passed, and the
pull request carries `lgtm` and `approved` with no `do-not-merge` label. A
merge queue only reads required checks, so each of the two becomes one, under
these names:

  - `Smoke gate`: the latest `pull-kube-agents-smoke-test` status on the pull
    request head is green (an `/override` included), or there is none and the
    pull request only changes paths the presubmit skips, which is when Tide
    does not wait for it.
  - `Merge labels`: Tide's query for this repository, read from the labels.

Tide also holds a merge on any other posted context that is not green, required
or not, because no `context_options` entry for this repository says otherwise.
These two checks do not restate that rule. The CLA needs no gate:
`cla/google` stays a required check, because the CLA app also posts it on a
queue commit (it subscribes to `merge_group`). A check that is not required stops
blocking a merge at cut-over unless the ruleset adds it.

Each check reports in two places, because the queue asks twice. A pull request
must already pass every required check to enter the queue, so `pull` and
`status` post the verdicts as commit statuses on its head (green ones only;
`post_verdicts` says why). A job's own check run cannot carry them: a run
started by a `status` event attaches to the default branch, not the head.
Then the queue runs every required check again on its own commit, and `group`
decides the same two for the pull request named in the
queue branch, as jobs that carry the check names. That judges only the last
pull request in a group, which is enough under the `ALLGREEN` grouping the
queue must use: every pull request ahead of it passed its own group first.
"""

import argparse
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from github_api import PER_PAGE, GitHubAPI, log  # noqa: E402

#: The queue is `main`'s alone; a release line still merges through Tide.
BASE = "main"
#: The scratch copy of `main` that the dry run puts a queue on before the cut-over.
DRY_RUN_BASE = "merge-queue-dry-run"
BASES = (BASE, DRY_RUN_BASE)
SMOKE_CONTEXT = "pull-kube-agents-smoke-test"
SMOKE_GATE = "Smoke gate"
LABELS_GATE = "Merge labels"
GATES = (SMOKE_GATE, LABELS_GATE)
#: Tide's query for this repository (`prow/oss/config.yaml` in oss-test-infra) wants both.
REQUIRED_LABELS = ("approved", "lgtm")
#: The four labels Tide's query for this repository excludes. Any other `do-not-merge/` label
#: does not hold a Tide merge, so it does not hold here either.
HOLD_LABELS = (
    "do-not-merge",
    "do-not-merge/hold",
    "do-not-merge/invalid-owners-file",
    "do-not-merge/work-in-progress",
)
#: `skip_if_only_changed` of `pull-kube-agents-smoke-test` in oss-test-infra
#: `prow/prowjobs/gke-labs/kube-agents/kube-agents-presubmits.yaml`. Keep the two identical.
SMOKE_SKIP_RE = re.compile(
    r"^(docs/|\.github/|examples/|tests/|scripts/release/|scripts/eval_dashboard/)"
    r"|^scripts/test_[^/]+\.py$|^[^/]+\.md$"
    r"|^(LICENSE|OWNERS|OWNERS_ALIASES|\.gitignore|install\.sh|upgrade\.sh|uninstall\.sh)$"
)
#: GitHub lists at most this many files for a pull request; a list that long may be cut short.
FILES_CEILING = 3000
#: The queue branch GitHub builds for a pull request: gh-readonly-queue/<base>/pr-<number>-<base sha>.
QUEUE_REF_RE = re.compile(r"^(?:refs/heads/)?gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]{40}$")
#: GitHub rejects a longer status description.
DESCRIPTION_MAX = 140
USER_AGENT = "kube-agents-merge-gate"
#: Where a status links when the run does not say; Actions always sets GITHUB_SERVER_URL.
DEFAULT_SERVER_URL = "https://github.com"
#: How much of a commit SHA a log line shows.
SHORT_SHA = 12

SUCCESS = "success"
PENDING = "pending"
FAILURE = "failure"


def labels_verdict(labels):
    """(state, description) for `Merge labels`. Pending, not failure: a label can still arrive."""
    holds = sorted(name for name in labels if name in HOLD_LABELS)
    if holds:
        return PENDING, f"held by {', '.join(holds)}"
    missing = [name for name in REQUIRED_LABELS if name not in labels]
    if missing:
        return PENDING, f"waiting for {' and '.join(missing)}"
    return SUCCESS, "lgtm and approved, no do-not-merge label"


def status_verdict(status, context):
    """(state, description) from the latest result on `context`; pending when there is none."""
    if status is None:
        return PENDING, f"waiting for {context}"
    state, _ = status
    if state == SUCCESS:
        return SUCCESS, f"{context} passed"
    if state == PENDING:
        return PENDING, f"{context} is running"
    return FAILURE, f"{context} is {state}"


def smoke_required(files):
    """Whether Prow runs the smoke on these paths. An overlong list counts as yes.

    An empty list counts as no, as it does for Prow's `skip_if_only_changed`.
    """
    if len(files) >= FILES_CEILING:
        return True
    return any(not SMOKE_SKIP_RE.search(path) for path in files)


def verdicts(pull, files):
    """{gate: (state, description)} for one pull request. `files` is called only if it is needed.

    `pull` has `labels` (a set of names) and `statuses` ({context: (state,
    description)}, the latest per context on the head). Pure apart from
    `files`, so the tests can drive it.
    """
    smoke = pull["statuses"].get(SMOKE_CONTEXT)
    # A posted smoke status decides, even on skipped paths: a hand-run `/test`
    # that went red holds a Tide merge too. The paths decide only when Prow
    # posted nothing.
    if smoke is None and not smoke_required(files()):
        smoke_gate = SUCCESS, "smoke not required: only paths it skips changed"
    else:
        smoke_gate = status_verdict(smoke, SMOKE_CONTEXT)
    return {
        SMOKE_GATE: smoke_gate,
        LABELS_GATE: labels_verdict(pull["labels"]),
    }


def latest_statuses(api, sha):
    """{context: (state, description)}: the latest status per context on a commit."""
    combined = api.get(f"/repos/{api.repo}/commits/{sha}/status?per_page={PER_PAGE}") or {}
    return {s["context"]: (s["state"], s.get("description") or "") for s in combined.get("statuses") or []}


def changed_files(api, number):
    """Every path the pull request touches, as Prow reads them: a rename counts by its new path only."""
    return [entry["filename"] for entry in api.get_all(f"/repos/{api.repo}/pulls/{number}/files")]


def rest_pull(api, raw):
    """The fields `verdicts` reads, from a REST pull request."""
    return {
        "number": raw["number"],
        "sha": raw["head"]["sha"],
        "base": raw["base"]["ref"],
        "labels": {label["name"] for label in raw.get("labels") or []},
        "statuses": latest_statuses(api, raw["head"]["sha"]),
    }


def post_verdicts(api, pull, results, dry_run=False):
    """Post each gate status that differs from the one on the head. Returns log lines.

    Green only. Tide holds a merge on any posted context that is not green,
    required or not, and nothing re-posts a status when the hold sweep's own
    label change starts no run, so a non-green posted while Tide still merges
    could hold a pull request after its inputs turned green again. A green that
    went stale is safe: the queue judges both again on its own commit.
    """
    lines = []
    target_url = None
    if os.environ.get("GITHUB_RUN_ID"):
        server = os.environ.get("GITHUB_SERVER_URL", DEFAULT_SERVER_URL)
        target_url = f"{server}/{api.repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    for gate in GATES:
        state, description = results[gate]
        description = description[:DESCRIPTION_MAX]
        current = pull["statuses"].get(gate)
        if current == (state, description):
            lines.append(f"#{pull['number']} {gate}: unchanged ({state})")
            continue
        if state != SUCCESS:
            lines.append(f"#{pull['number']} {gate}: not posted, not green: {state}: {description}")
            continue
        if not dry_run:
            payload = {"state": state, "context": gate, "description": description}
            if target_url:
                payload["target_url"] = target_url
            api.post(f"/repos/{api.repo}/statuses/{pull['sha']}", payload)
        verb = "would post" if dry_run else "posted"
        lines.append(f"#{pull['number']} {gate}: {verb} {state}: {description}")
    return lines


def gate_pull(api, pull, dry_run=False):
    """Decide and post the two statuses for one pull request on `main`."""
    if pull["base"] not in BASES:
        return [f"#{pull['number']}: targets {pull['base']}, not {BASE}; left to Tide"]
    results = verdicts(pull, lambda: changed_files(api, pull["number"]))
    return post_verdicts(api, pull, results, dry_run=dry_run)


def cmd_pull(api, number, dry_run=False):
    return gate_pull(api, rest_pull(api, api.get(f"/repos/{api.repo}/pulls/{number}")), dry_run=dry_run)


def cmd_status(api, sha, dry_run=False):
    """Gate every open pull request whose head is `sha`. Listed, not searched: search lags."""
    lines = []
    for raw in api.get_all(f"/repos/{api.repo}/pulls?state=open"):
        if raw["head"]["sha"] == sha and raw["base"]["ref"] in BASES:
            lines += gate_pull(api, rest_pull(api, raw), dry_run=dry_run)
    return lines or [f"{sha[:SHORT_SHA]}: not the head of an open pull request on {BASE}"]


def cmd_group(api, ref, gate):
    """(passed, line) for one gate on a queue commit, judged on the pull request it names."""
    match = QUEUE_REF_RE.match(ref or "")
    if not match:
        return False, f"{ref!r} is not a merge queue branch"
    number = int(match.group(1))
    pull = rest_pull(api, api.get(f"/repos/{api.repo}/pulls/{number}"))
    state, description = verdicts(pull, lambda: changed_files(api, number))[gate]
    return state == SUCCESS, f"#{number} {gate}: {state}: {description}"


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name")
    parser.add_argument("--dry-run", action="store_true", help="log what would change, change nothing")
    sub = parser.add_subparsers(dest="command", required=True)
    pull = sub.add_parser("pull", help="post the gate statuses on one pull request's head")
    pull.add_argument("--number", type=int, required=True)
    status = sub.add_parser("status", help="post the gate statuses on the pull requests whose head is SHA")
    status.add_argument("--sha", required=True)
    group = sub.add_parser("group", help="judge one gate on a merge queue commit; exit 1 unless it passes")
    group.add_argument("--ref", required=True, help="the queue branch, github.event.merge_group.head_ref")
    group.add_argument("--gate", required=True, choices=GATES)
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not args.repo or not token:
        print("GITHUB_REPOSITORY (or --repo) and GITHUB_TOKEN are required", file=sys.stderr)
        return 2
    api = GitHubAPI(args.repo, token, user_agent=USER_AGENT)
    failures = 0
    if args.command == "pull":
        lines = cmd_pull(api, args.number, dry_run=args.dry_run)
    elif args.command == "status":
        lines = cmd_status(api, args.sha, dry_run=args.dry_run)
    else:
        passed, line = cmd_group(api, args.ref, args.gate)
        lines, failures = [line], 0 if passed else 1
    for line in lines:
        log(line)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
