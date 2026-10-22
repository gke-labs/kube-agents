#!/usr/bin/env python3
"""The merge queue's two gate checks, and the sweep that enqueues pull requests.

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

A smoke green from before the blocking job changed what it grades does not
pass. While the repository variable SMOKE_PIN_NOT_BEFORE holds a UTC time, the
same cutoff `pin_smoke_status.py` applies to its re-pins, `Smoke gate` stays
pending on a green whose build first reported before that time, until a run
from after it replaces it. The time is the earliest success status on the
commit naming the same build, as that script reads it: a re-pin is a fresh copy
with its own creation time. A green with no build link counts from the earliest
success with none. That is one more read per green while the variable is set,
skipped when the head `Smoke gate` is already green.
An `/override` links its own comment, so it counts from when it was posted. A
green that `pin_smoke_status.py` expired carries its note and stays pending
even after the variable is cleared. A green from before the cutoff that the
script did not expire, because Tide already saw it as stale, has no note. So
leave the variable set: clearing it lets such a green pass again. An
unparsable value decides nothing and exits non-zero on every path that reads
it. `Merge labels` does not read it, but on the queue the `Smoke gate` leg
fails each group, so check the value before you set it. Only greens are posted on
a head, so a head `Smoke gate` posted green before the cutoff stays green: the
pull request enters the queue, fails its group there, and like any pull request
the queue removed, stays out until its head moves: the one retry after a
`failed_checks` removal fails the same way. So the pending text asks for a push,
not a `/test`.

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

`sweep` is the queue's way in. No author here has write access, the robot
included, so nobody can press "Merge when ready". For every pull request on
`main` that passes the label test it turns auto-merge on, which with a queue
means "enqueue once the required checks pass", and it takes a pull request
that lost a label or gained a hold back out. It also re-posts the head
statuses of those pull requests, in case an event was missed. The auto-merge
and dequeue calls use a GitHub App token, `ENQUEUE_TOKEN`: GitHub starts no
workflow run for an event `GITHUB_TOKEN` causes, so a queue entry it made could
get no `merge_group` runs, and turning on auto-merge needs `contents: write`
(and `workflows: write` for a pull request that changes a workflow file).

A pull request the queue removed, or that someone took out by hand, stays out
until its head moves (its first check suite is newer than the removal): its head checks are still green, so turning auto-merge on
again would put the same failing tree back in the queue every five minutes.
One exception: after the first `failed_checks` removal since the push, the
sweep puts it back once, because a flaky or cancelled check gets the same
reason as a real failure. A second one keeps it out.
The sweep posts one comment on such a pull request, with the reason and what
to do for that reason. The first step is always a push: a fix, a rebase, or an
empty commit. For a failed check, or a reason the comment does not name, a
write user's re-add command follows if the push does not work.
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta
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
#: The repository variable that holds the smoke cutoff (module docstring), read as
#: `pin_smoke_status.py` reads it.
NOT_BEFORE_ENV = "SMOKE_PIN_NOT_BEFORE"
#: What `Smoke gate` says while it waits on a green from before the cutoff.
SMOKE_PREDATES = f"smoke predates {NOT_BEFORE_ENV}; push a new commit to rerun {SMOKE_CONTEXT}"
#: The note `pin_smoke_status.py` (EXPIRED_NOTE) adds to a pre-cutoff green that Tide still
#: credited. It holds that green with or without the variable, as it does for Tide.
SMOKE_EXPIRED_NOTE = f"(predates {NOT_BEFORE_ENV}: retest)"
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
#: `trusted_team_for_sticky_lgtm` for this repository (`prow/oss/plugins.yaml` in oss-test-infra):
#: Prow keeps `lgtm` across a push only for its members.
STICKY_LGTM_TEAM = "gke-labs/Googlers"
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
ENABLE = "enable"
DISABLE = "disable"
LEFT_OUT = "left out"
#: What a left-out line says when the removal event carries no reason or actor.
UNKNOWN_REMOVAL_FIELD = "unknown"
#: The removal reason GitHub writes for a failed or cancelled `merge_group` check. A flake and a
#: real failure both get it, so the sweep puts the pull request back once per head commit.
FAILED_CHECKS_REASON = "failed_checks"
#: Opens the comment that tells the author why the sweep left the pull request out. Keyed on the
#: removal's time, so each removal gets one comment and a later removal gets a new one.
LEFT_OUT_MARKER = "<!-- merge-queue-left-out {removed} -->"
LEFT_OUT_COMMENT = """{marker}
{headline} The sweep logged:

```text
{line}
```

{advice}

A push keeps `lgtm` for members of {team}. Other authors may need a new `/lgtm`.
"""
#: The GraphQL type of an actor the comment may @-mention; an App or an unknown actor is named
#: without the `@`, so the comment pings no account that only shares its name.
USER_ACTOR_TYPE = "User"
#: The other two removal reasons the comment answers by name.
MANUAL_REASON = "manual"
MERGE_CONFLICT_REASON = "merge_conflict"
#: The commands the comment hands the author and a write user.
EMPTY_PUSH_COMMAND = 'git commit --allow-empty -m "Re-enter the merge queue" && git push'
ENABLE_COMMAND = "gh api graphql -f query='{query}'"
#: The comment's first line and advice, per removal reason. The sweep posts it only when the pull
#: request stays out, so a `failed_checks` one has already had its retry.
LEFT_OUT_HEADLINES = {
    FAILED_CHECKS_REASON: "This pull request was removed from the merge queue because a required check failed again, "
    "after the one retry the enqueue sweep gives it.",
    MANUAL_REASON: "This pull request was removed from the merge queue by {who}.",
    MERGE_CONFLICT_REASON: "This pull request was removed from the merge queue because it conflicts with `main`, "
    "or with a pull request ahead of it in the queue.",
}
LEFT_OUT_HEADLINE_OTHER = "This pull request was removed from the merge queue (reason: {why})."
LEFT_OUT_ADVICE = {
    FAILED_CHECKS_REASON: """If a check found a real problem, push a fix.

If it was a flake, push an empty commit, and the sweep puts it back once its checks pass:

```shell
{push}
```

If that does not work, ask a write user to run this command:

```shell
{enable}
```""",
    MANUAL_REASON: """If {who} did not mean to keep it out, push (an empty commit is enough), and the sweep puts \
it back once its checks pass:

```shell
{push}
```

If they did mean to keep it out, they should add a hold label (`/hold`), so that it stays out after \
the next push.""",
    MERGE_CONFLICT_REASON: "Rebase onto `main`, fix any conflict, and push. The sweep puts it back after its checks pass.",
}
LEFT_OUT_ADVICE_OTHER = """Push a fix, or an empty commit, and the sweep puts it back once its checks pass:

```shell
{push}
```

If that does not work, ask a write user to run this command:

```shell
{enable}
```"""
#: The sweep's own login when `--actor` names no App: `GITHUB_TOKEN`'s, which makes no queue
#: call. A write run always passes the App's slug, whose own dequeue of a held pull request does
#: not keep it out; a dry run has no App token, so it may report that dequeue as a removal.
SWEEP_ACTOR = "github-actions"
#: The suffix REST puts on a bot's login and GraphQL leaves off.
BOT_SUFFIX = "[bot]"
#: How long after the head's first check suite a queue removal still counts as caused by that
#: push: a push to a queued pull request removes it, and the two land within seconds.
PUSH_REMOVAL_WINDOW = timedelta(minutes=2)
#: Enough of the head's check suites to find the first; Actions and the CLA app each add one.
CHECK_SUITES_READ = 10
#: Queue removals read per pull request: enough to count the `failed_checks` ones since the push.
REMOVALS_READ = 10
#: How many times per head commit the sweep puts a pull request back after `failed_checks`
#: removals. `LEFT_OUT_HEADLINES` and the sweep's log line say "one retry"; change them with it.
FAILED_CHECKS_RETRIES = 1

#: The smoke and the gates are commit statuses; the rollup also carries check runs, which this skips.
PULLS_QUERY = """
query($owner: String!, $name: String!, $base: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, baseRefName: $base, first: 50, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        id number isDraft headRefOid
        labels(first: 100) { nodes { name } }
        autoMergeRequest { enabledAt }
        mergeQueueEntry { id }
        timelineItems(last: %(removals)d, itemTypes: [REMOVED_FROM_MERGE_QUEUE_EVENT]) {
          nodes { ... on RemovedFromMergeQueueEvent { createdAt reason actor { __typename login } } }
        }
        commits(last: 1) { nodes { commit {
          checkSuites(first: %(suites)d) { nodes { createdAt } }
          statusCheckRollup { contexts(first: 100) { nodes {
          __typename
          ... on StatusContext { context state description }
        } } } } } }
      }
    }
  }
}
"""
ENABLE_MUTATION = "mutation($id: ID!) { enablePullRequestAutoMerge(input: {pullRequestId: $id}) { clientMutationId } }"
#: The enable call with the pull request's node id written in, for a write user to paste.
ENABLE_COMMAND_QUERY = 'mutation {{ enablePullRequestAutoMerge(input: {{pullRequestId: "{id}"}}) {{ clientMutationId }} }}'
DISABLE_MUTATION = "mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) { clientMutationId } }"
DEQUEUE_MUTATION = "mutation($id: ID!) { dequeuePullRequest(input: {id: $id}) { clientMutationId } }"
#: The GraphQL type name of a commit status in a rollup.
STATUS_CONTEXT = "StatusContext"


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


def verdicts(pull, files, not_before=None, first_reported=None):
    """{gate: (state, description)} for one pull request. `files` is called only if it is needed.

    `pull` has `labels` (a set of names) and `statuses` ({context: (state,
    description)}, the latest per context on the head). `first_reported` is
    called only for a green smoke while `not_before` is set: when its build
    first reported green, None when that is unknown. Pure apart from the two
    callables, so the tests can drive it.
    """
    smoke = pull["statuses"].get(SMOKE_CONTEXT)
    # A posted smoke status decides, even on skipped paths: a hand-run `/test`
    # that went red holds a Tide merge too. The paths decide only when Prow
    # posted nothing.
    if smoke is None and not smoke_required(files()):
        smoke_gate = SUCCESS, "smoke not required: only paths it skips changed"
    else:
        smoke_gate = status_verdict(smoke, SMOKE_CONTEXT)
        if smoke_gate[0] == SUCCESS and SMOKE_EXPIRED_NOTE in smoke[1]:
            smoke_gate = PENDING, SMOKE_PREDATES
        elif smoke_gate[0] == SUCCESS and not_before is not None:
            reported = first_reported()
            if reported is None or reported < not_before:
                smoke_gate = PENDING, SMOKE_PREDATES
    return {
        SMOKE_GATE: smoke_gate,
        LABELS_GATE: labels_verdict(pull["labels"]),
    }


def enqueue_action(pull):
    """ENABLE, DISABLE, LEFT_OUT or None: what the sweep does with one pull request's auto-merge."""
    wanted = not pull["draft"] and labels_verdict(pull["labels"])[0] == SUCCESS
    queued = pull["auto_merge"] or pull["queue_entry"] is not None
    if wanted and not queued:
        return LEFT_OUT if pull.get("removed_since_push") and not pull.get("retry") else ENABLE
    if queued and not wanted:
        return DISABLE
    return None


def latest_statuses(api, sha):
    """{context: (state, description)}: the latest status per context on a commit."""
    combined = api.get(f"/repos/{api.repo}/commits/{sha}/status?per_page={PER_PAGE}") or {}
    return {s["context"]: (s["state"], s.get("description") or "") for s in combined.get("statuses") or []}


def smoke_first_reported(api, sha):
    """When the build behind the latest smoke status on `sha` first reported green; None when unknown.

    The earliest success status naming the same build (its target URL); for a
    status with no link, the earliest success with none, because each re-pin
    copies it with a new creation time. None
    when the latest smoke status is not green or a creation time is unreadable
    or zoneless, which the cutoff counts as before it.
    """
    smoke = [s for s in api.get_all(f"/repos/{api.repo}/commits/{sha}/statuses") if s.get("context") == SMOKE_CONTEXT]
    if not smoke or smoke[0].get("state") != SUCCESS:
        return None
    url = smoke[0].get("target_url") or ""
    copies = [s for s in smoke if s.get("state") == SUCCESS and (s.get("target_url") or "") == url]
    try:
        times = [parse_time(s.get("created_at") or "") for s in copies]
    except ValueError:
        return None
    return None if any(t.tzinfo is None for t in times) else min(times)


def not_before_from_env(environ=os.environ):
    """The cutoff from NOT_BEFORE_ENV, None when unset or empty; ValueError when unparsable or zoneless."""
    raw = (environ.get(NOT_BEFORE_ENV) or "").strip()
    if not raw:
        return None
    parsed = parse_time(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"{raw!r} carries no time zone")
    return parsed


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


def gate_pull(api, pull, dry_run=False, not_before=None):
    """Decide and post the two statuses for one pull request on `main`."""
    if pull["base"] not in BASES:
        return [f"#{pull['number']}: targets {pull['base']}, not {BASE}; left to Tide"]
    # Only greens are posted, so a head `Smoke gate` that is green stays green whatever the
    # cutoff says. Do not read the build history for it on each sweep.
    if (pull["statuses"].get(SMOKE_GATE) or (None,))[0] == SUCCESS:
        not_before = None
    results = verdicts(
        pull, lambda: changed_files(api, pull["number"]), not_before, lambda: smoke_first_reported(api, pull["sha"])
    )
    return post_verdicts(api, pull, results, dry_run=dry_run)


def cmd_pull(api, number, dry_run=False, not_before=None):
    return gate_pull(api, rest_pull(api, api.get(f"/repos/{api.repo}/pulls/{number}")), dry_run=dry_run, not_before=not_before)


def cmd_status(api, sha, dry_run=False, not_before=None):
    """Gate every open pull request whose head is `sha`. Listed, not searched: search lags."""
    lines = []
    for raw in api.get_all(f"/repos/{api.repo}/pulls?state=open"):
        if raw["head"]["sha"] == sha and raw["base"]["ref"] in BASES:
            lines += gate_pull(api, rest_pull(api, raw), dry_run=dry_run, not_before=not_before)
    return lines or [f"{sha[:SHORT_SHA]}: not the head of an open pull request on {BASE}"]


def cmd_group(api, ref, gate, not_before=None):
    """(passed, line) for one gate on a queue commit, judged on the pull request it names."""
    match = QUEUE_REF_RE.match(ref or "")
    if not match:
        return False, f"{ref!r} is not a merge queue branch"
    number = int(match.group(1))
    pull = rest_pull(api, api.get(f"/repos/{api.repo}/pulls/{number}"))
    results = verdicts(pull, lambda: changed_files(api, number), not_before, lambda: smoke_first_reported(api, pull["sha"]))
    state, description = results[gate]
    return state == SUCCESS, f"#{number} {gate}: {state}: {description}"


def rollup_statuses(nodes):
    """{context: (state, description)} from a GraphQL rollup, read as `latest_statuses` reads REST.

    GraphQL spells states in capitals; the REST form is what gets compared and
    posted.
    """
    return {
        node["context"]: (node["state"].lower(), node.get("description") or "")
        for node in nodes
        if node.get("__typename") == STATUS_CONTEXT
    }


def _pushed_at(node):
    """When the head was pushed: its first check suite, or None. The timeline places a commit by
    its commit date, not by when it was pushed."""
    commits = node["commits"]["nodes"]
    suites = ((commits[0]["commit"].get("checkSuites") or {}).get("nodes") or []) if commits else []
    pushed = [parse_time(suite["createdAt"]) for suite in suites if suite.get("createdAt")]
    return min(pushed) if pushed else None


def _after_push(removal, pushed, actor):
    """Whether a removal came from someone other than the sweep, after the push and not caused by it."""
    if not removal.get("createdAt"):
        return False
    login = ((removal.get("actor")) or {}).get("login") or ""
    if login.removesuffix(BOT_SUFFIX) == actor.removesuffix(BOT_SUFFIX):
        return False
    return parse_time(removal["createdAt"]) - pushed > PUSH_REMOVAL_WINDOW


def removed_since_push(node, actor=SWEEP_ACTOR):
    """Whether someone other than the sweep removed the pull request from the queue after its last push.

    Only the last removal counts. A head with no check suite counts as pushed
    after any removal.
    """
    removals = ((node.get("timelineItems") or {}).get("nodes")) or []
    pushed = _pushed_at(node)
    return bool(removals and pushed and _after_push(removals[-1], pushed, actor))


def retry_due(node, actor=SWEEP_ACTOR):
    """Whether the sweep puts a removed pull request back once: the last removal is `failed_checks`
    and it is the only `failed_checks` removal since the push.

    A flake and a real failure get the same reason, so one retry per head
    commit gives a flake its way back, and a second failure keeps the pull
    request out. A `manual` or `merge_conflict` removal is never retried.

    The hold race shares the one retry: a held pull request at the front of the
    queue fails its own `Merge labels` job and gets `failed_checks` too. So
    after `/hold cancel` it comes back only if that was the first
    `failed_checks` removal since the push; a flake later on the same head then
    keeps it out.
    """
    if not removed_since_push(node, actor):
        return False
    removals = node["timelineItems"]["nodes"]
    if removals[-1].get("reason") != FAILED_CHECKS_REASON:
        return False
    pushed = _pushed_at(node)
    failed = [r for r in removals if r.get("reason") == FAILED_CHECKS_REASON and _after_push(r, pushed, actor)]
    return len(failed) <= FAILED_CHECKS_RETRIES


def last_removal(node):
    """The last queue removal on a pull request's timeline, or None."""
    removals = ((node.get("timelineItems") or {}).get("nodes")) or []
    return removals[-1] if removals else None


def left_out_line(pull):
    """The sweep's line for a pull request it leaves out: why, and the command that puts it back.

    The command is the sweep's own enable call, run on a write user's token: a
    write user pastes it to turn auto-merge on, which with the queue means
    "enqueue once the required checks pass". Once auto-merge is on, the next
    sweep leaves the pull request alone.
    """
    removal = pull.get("removal") or {}
    when = removal.get("createdAt") or UNKNOWN_REMOVAL_FIELD
    who = ((removal.get("actor")) or {}).get("login") or UNKNOWN_REMOVAL_FIELD
    why = removal.get("reason") or UNKNOWN_REMOVAL_FIELD
    enable = ENABLE_COMMAND_QUERY.format(id=pull["id"])
    return (
        f"#{pull['number']}: left out, removed {when} by {who} (reason: {why}). "
        f"To re-add (a write user): gh api graphql -f query='{enable}', "
        f"or push a fix (a push drops lgtm unless the author is in {STICKY_LGTM_TEAM})."
    )


def left_out_comment(pull, marker, line):
    """The comment body for a pull request the sweep leaves out: why, the logged line, what to do."""
    removal = pull.get("removal") or {}
    actor = removal.get("actor") or {}
    login = actor.get("login")
    why = removal.get("reason") or UNKNOWN_REMOVAL_FIELD
    if login and actor.get("__typename") == USER_ACTOR_TYPE:
        who = f"@{login}"
    else:
        who = f"`{login.removesuffix(BOT_SUFFIX)}`" if login else UNKNOWN_REMOVAL_FIELD
    fields = {
        "who": who,
        "why": why,
        "push": EMPTY_PUSH_COMMAND,
        "enable": ENABLE_COMMAND.format(query=ENABLE_COMMAND_QUERY.format(id=pull["id"])),
    }
    headline = LEFT_OUT_HEADLINES.get(why, LEFT_OUT_HEADLINE_OTHER).format(**fields)
    advice = LEFT_OUT_ADVICE.get(why, LEFT_OUT_ADVICE_OTHER).format(**fields)
    return LEFT_OUT_COMMENT.format(marker=marker, headline=headline, line=line, advice=advice, team=STICKY_LGTM_TEAM)


def comment_left_out(api, pull, line, dry_run=False):
    """Post the left-out comment, with `line` in it, once on a pull request the sweep leaves out.

    Returns a log line.

    One comment per removal: the sweep runs every five minutes, so it reads the
    comments since the removal first and skips a pull request that already has
    this removal's marker. The read happens only for pull requests the sweep
    leaves out. A failed read or post is logged and not counted as a failure:
    the left-out line is already in the log, and a red every five minutes for
    a notice would hide real failures.
    """
    removed = (pull.get("removal") or {}).get("createdAt")
    marker = LEFT_OUT_MARKER.format(removed=removed or UNKNOWN_REMOVAL_FIELD)
    path = f"/repos/{api.repo}/issues/{pull['number']}/comments"
    try:
        # The marker comment is newer than the removal, so only comments since then can hold it.
        since = f"{path}?since={removed}" if removed else path
        if any((comment.get("body") or "").startswith(marker) for comment in api.get_all(since)):
            return f"#{pull['number']}: left-out comment already posted"
        if not dry_run:
            body = left_out_comment(pull, marker, line)
            api.post(path, {"body": body})
    except Exception as error:  # a notice that fails must not turn every sweep red
        return f"#{pull['number']}: left-out comment not posted: {error}"
    return f"#{pull['number']}: {'would post' if dry_run else 'posted'} the left-out comment"


def parse_time(stamp):
    """A GitHub timestamp, `2026-10-13T20:13:25Z`, as an aware datetime."""
    return datetime.fromisoformat(stamp)


def graphql_pulls(api, base=BASE, actor=SWEEP_ACTOR):
    """Every open pull request on `base`, with what the sweep reads, in one query per 50."""
    owner, name = api.repo.split("/", 1)
    pulls = []
    after = None
    while True:
        page = api.graphql(PULLS_QUERY % {"removals": REMOVALS_READ, "suites": CHECK_SUITES_READ}, {"owner": owner, "name": name, "base": base, "after": after})
        page = page["repository"]["pullRequests"]
        for node in page["nodes"]:
            commits = node["commits"]["nodes"]
            rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
            pulls.append({
                "id": node["id"],
                "number": node["number"],
                "sha": node["headRefOid"],
                "base": base,
                "draft": node["isDraft"],
                "labels": {label["name"] for label in node["labels"]["nodes"]},
                "auto_merge": node["autoMergeRequest"] is not None,
                "queue_entry": (node["mergeQueueEntry"] or {}).get("id"),
                "removed_since_push": removed_since_push(node, actor),
                "retry": retry_due(node, actor),
                "removal": last_removal(node),
                "statuses": rollup_statuses(((rollup or {}).get("contexts") or {}).get("nodes") or []),
            })
        if not page["pageInfo"]["hasNextPage"]:
            return pulls
        after = page["pageInfo"]["endCursor"]


def sweep(api, base=BASE, enqueuer=None, actor=SWEEP_ACTOR, dry_run=False, not_before=None):
    """Re-post head statuses and reconcile auto-merge for every pull request. (lines, failures).

    `api` reads, posts statuses, and posts the left-out comment; `enqueuer`,
    the App token, makes the auto-merge and dequeue calls.

    Statuses are refreshed only for pull requests that pass the label test:
    those are the ones the queue can take, and refreshing every open pull
    request every five minutes would spend most of `GITHUB_TOKEN`'s hourly
    allowance.
    """
    enqueuer = enqueuer or api
    lines = []
    failures = 0
    for pull in graphql_pulls(api, base, actor):
        try:
            if not pull["draft"] and labels_verdict(pull["labels"])[0] == SUCCESS:
                lines += gate_pull(api, pull, dry_run=dry_run, not_before=not_before)
            action = enqueue_action(pull)
            if action == ENABLE and not dry_run:
                enqueuer.graphql(ENABLE_MUTATION, {"id": pull["id"]})
            elif action == DISABLE and not dry_run:
                if pull["queue_entry"]:
                    enqueuer.graphql(DEQUEUE_MUTATION, {"id": pull["id"]})
                if pull["auto_merge"]:
                    enqueuer.graphql(DISABLE_MUTATION, {"id": pull["id"]})
            if action:
                if action == LEFT_OUT:
                    line = left_out_line(pull)
                    lines.append(line)
                    lines.append(comment_left_out(api, pull, line, dry_run=dry_run))
                else:
                    verb = {ENABLE: "auto-merge on", DISABLE: "auto-merge off and out of the queue"}[action]
                    if action == ENABLE and pull.get("retry"):
                        verb += f" again, the one retry after a {FAILED_CHECKS_REASON} removal"
                    lines.append(f"#{pull['number']}: {'would turn ' if dry_run else ''}{verb}")
        except Exception as error:  # one pull request must not stop the sweep
            failures += 1
            lines.append(f"#{pull['number']}: failed: {error}")
    return lines, failures


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
    sweep_ = sub.add_parser("sweep", help="refresh statuses and reconcile auto-merge for every pull request on a base")
    sweep_.add_argument("--base", default=BASE, choices=BASES)
    sweep_.add_argument("--actor", default=SWEEP_ACTOR, help="the login ENQUEUE_TOKEN acts as: the App's slug")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN")
    if not args.repo or not token:
        print("GITHUB_REPOSITORY (or --repo) and GITHUB_TOKEN are required", file=sys.stderr)
        return 2
    not_before = None
    # `Merge labels` never reads the cutoff, so a bad value must not fail it on the queue too.
    if not (args.command == "group" and args.gate == LABELS_GATE):
        try:
            not_before = not_before_from_env()
        except ValueError as error:
            print(f"{NOT_BEFORE_ENV} is not a UTC time: {error}", file=sys.stderr)
            return 2
    api = GitHubAPI(args.repo, token, user_agent=USER_AGENT)
    failures = 0
    if args.command == "pull":
        lines = cmd_pull(api, args.number, dry_run=args.dry_run, not_before=not_before)
    elif args.command == "status":
        lines = cmd_status(api, args.sha, dry_run=args.dry_run, not_before=not_before)
    elif args.command == "group":
        passed, line = cmd_group(api, args.ref, args.gate, not_before=not_before)
        lines, failures = [line], 0 if passed else 1
    else:
        enqueue_token = os.environ.get("ENQUEUE_TOKEN")
        if not enqueue_token and not args.dry_run:
            print("ENQUEUE_TOKEN is required unless --dry-run", file=sys.stderr)
            return 2
        enqueuer = GitHubAPI(args.repo, enqueue_token, user_agent=USER_AGENT) if enqueue_token else None
        lines, failures = sweep(api, args.base, enqueuer, args.actor, dry_run=args.dry_run, not_before=not_before)
    for line in lines:
        log(line)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
