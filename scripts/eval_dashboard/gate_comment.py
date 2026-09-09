#!/usr/bin/env python3
"""Comment on a pull request whose smoke-gate run went red: what failed, and
whether it is the gate's or the pull request's.

The gate's own status says "failed" and nothing else; the author then opens
a two-hour build log to learn whether the crashloop trio that redded them
is the same trio redding everyone (#1278) or their own ImagePullBackOff.
This is the health job answering that on the pull request itself, every
tick, for the runs that finished since the last tick:

    ### ❌ Smoke gate: failed · 3 of 14 cases
    > 🔴 Gate outage in progress since Sun 7:30 AM ET. ... not your code.
    | Case | Result | Also failing on |
    11 cases passed. Run 132 min on evals-23 · build log

Which runs: pull-kube-agents-smoke-test builds in data.json that finished
after the state file's `last_comment_tick`, concluded FAILURE, and graded at
least one repetition -- so an aborted run, a setup death, or a suite that
lost every repetition to a storm gets no comment (the Chat space and the
dashboard carry those). A red is either a gate case failing every graded
repetition, or a hard failure: FAILURE with no such case, which is an
absolute check (a forbidden cluster change, a verifier that errored) or a
truncated log, and says so.

Which words: classify.py's `classify_run` -- the same rules the dashboard's
run.html and the incident brief use -- decides per case whether it is
`shared` (the gate's), `only-this-pr` (yours), `storm` or unexplained; this
module only phrases it.

One comment per pull request, found by a hidden marker and edited in place
on later runs; a build already commented on is never commented on twice.
Posting is `gh api` with the workflow's GITHUB_TOKEN (ghcli.py); a failure
is a warning and that run is retried next tick, and nothing here fails the
job.

Run:  python3 scripts/eval_dashboard/gate_comment.py --data data.json --health health.json --state gate-comment-state.json --dry-run
Test: cd scripts && python3 -m unittest test_eval_dashboard_gate_comment
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

try:
    from eval_dashboard import classify, ghcli, health, post_health
except ImportError:  # run as a script: scripts/eval_dashboard/gate_comment.py
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from eval_dashboard import classify, ghcli, health, post_health

STATE_SCHEMA_VERSION = 1
# The hidden first line every comment starts with; how the next tick finds
# it to edit rather than post again.
MARKER = "<!-- smoke-gate-comment -->"
JOB_NAME = "pull-kube-agents-smoke-test"
# The first tick ever has no `last_comment_tick`; it looks back this far
# rather than commenting on two weeks of history.
FIRST_TICK_LOOKBACK = timedelta(hours=1)
# How long a pull request's entry stays in the state file after its last
# comment; data.json itself keeps 14 days.
STATE_RETENTION = timedelta(days=14)
# A failed post is retried next tick by moving the watermark back to just
# before that run's finish.
RETRY_BACKOFF = timedelta(seconds=1)

# classify.py's vocabulary, by name so a rename there is a NameError here.
CLS_SHARED = classify.CLS_SHARED
CLS_ONLY_THIS_PR = classify.CLS_ONLY_THIS_PR
CLS_STORM = classify.CLS_STORM
OUTCOME_PASSED = classify.OUTCOME_PASSED
OUTCOME_PARTIAL = classify.OUTCOME_PARTIAL
OUTCOME_FAILED = classify.OUTCOME_FAILED
# "passed on the last N runs from other PRs" counts inside classify.py's
# only-this-PR window.
ONLY_PR_WINDOW = classify.ONLY_PR_WINDOW
EXCERPT_CHARS = 160

# Links. The run page and the brief are the dashboard's (post_health owns
# the brief's contract); the build log is Prow's Deck for this job.
DASHBOARD_ROOT = post_health.DASHBOARD_URL.rsplit("/", 1)[0]
RUN_URL = DASHBOARD_ROOT + "/run.html?build={build_id}"
BUILD_LOG_URL = "https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/{pr}/" + JOB_NAME + "/{build_id}"
# "kube-agents-evals-23" reads as "evals-23".
PROJECT_PREFIX = "kube-agents-"
UNKNOWN_PROJECT = "an unknown project"

# Wording. Plain words for whoever is deciding whether to type /retest.
HEADING = "### ❌ Smoke gate: failed · {failed} of {total} cases"
HEADING_HARD = "### ❌ Smoke gate: failed · hard failure"
BOX_OUTAGE = "🔴 **Gate outage in progress** since {since}. {what} fail on every PR ({prs} PRs so far)."
BOX_DEGRADED = "🟡 **Gate degraded** since {since}. {cause}"
BOX_HEALTHY = "🟢 **Gate healthy.**"
ALL_THEIRS = "**Your {n} {failures} {are} exactly {those}, so this red is not your code.** Don't retest yet; you'll be retested automatically once the fix is confirmed."
SOME_THEIRS = "**{theirs} of your {n} failures {are_theirs} the gate's ({their_cases}); {yours_text}.** Fix {that}; don't retest for the rest until the gate is healthy."
YOURS_ONLY = "{case} passed on the last {elsewhere} {runs} from other PRs and failed on your last {streak}."
LOOKS_YOURS = "**This looks specific to your PR.**"
LOOK_YOURS = "**These look specific to your PR.**"
SHARED_NO_INCIDENT = "{case} is also failing on {also} right now, so it may not be your code; no outage is declared yet."
UNEXPLAINED = "{case} failed here and nothing on other PRs matches it yet; read the transcript."
HARD_FAILURE = (
    "🔴 **The run failed without a gate case failing all of its repetitions.** An absolute check tripped"
    " (a forbidden cluster change, a verifier that errored) or the log was cut short; the build log has the reason."
)
HELD_OUT_NOTE = " {n} held-out {cases} also failed; held-out cases do not block."
LINK_RUN = "[Why this run failed →]({url})"
LINK_BRIEF = "[Incident brief →]({url})"
TABLE_HEAD = "| Case | Result | Also failing on |\n| --- | --- | --- |"
TABLE_ROW = "| `{case}`{note} | {result} | {also} |"
HELD_OUT_CELL = " (held out)"
REASON_LINE = "Reason: `{reason}`"
REASON_LINE_NAMED = "Reason (`{case}`): `{reason}`"
FOOTER = "{passed} {cases} passed. Run {minutes} min on {project} · [build log]({url})"
NO_OTHER_PR = "no other PR"
OTHER_PRS = "{n} other {prs}"

UTC = timezone.utc


def log(message: str) -> None:
    print(message, file=sys.stderr)


def plural(count: int, one: str, many: str | None = None) -> str:
    return one if count == 1 else (many or one + "s")


# --------------------------------------------------------------------------- #
# Selecting runs
# --------------------------------------------------------------------------- #


class Red:
    """One red run and what the comment needs to know about it."""

    def __init__(self, raw: dict, verdict: dict, admitted: frozenset):
        self.raw = raw
        self.run = health.Run(raw)
        self.cases = [c for c in verdict.get("cases") or [] if c.get("outcome")]
        for case in self.cases:
            case["admitted"] = case.get("admitted", True) and (not admitted or case["case"] in admitted)
        self.failed = [c for c in self.cases if c["outcome"] == OUTCOME_FAILED and c["admitted"]]
        self.held_out_failed = [c for c in self.cases if c["outcome"] == OUTCOME_FAILED and not c["admitted"]]
        self.matches_incident = bool(verdict.get("matches_incident"))

    @property
    def hard(self) -> bool:
        return not self.failed

    @property
    def graded(self) -> list[dict]:
        return [c for c in self.cases if c["outcome"] in (OUTCOME_PASSED, OUTCOME_PARTIAL, OUTCOME_FAILED)]

    @property
    def passed(self) -> int:
        return sum(1 for c in self.graded if c["outcome"] in (OUTCOME_PASSED, OUTCOME_PARTIAL))

    def theirs(self) -> list[dict]:
        return [c for c in self.failed if c.get("cls") in (CLS_SHARED, CLS_STORM)]

    def yours(self) -> list[dict]:
        return [c for c in self.failed if c.get("cls") == CLS_ONLY_THIS_PR]

    def unclear(self) -> list[dict]:
        return [c for c in self.failed if c.get("cls") not in (CLS_SHARED, CLS_STORM, CLS_ONLY_THIS_PR)]


def is_red(run: health.Run) -> bool:
    """FAILURE, with tasks, and at least one graded repetition: not aborted,
    not a setup death, not a suite the storm emptied."""
    return run.result == health.RUN_FAILURE and run.full and any(task.graded for task in run.tasks)


def newest_red_per_pr(data: dict, since: datetime, now: datetime) -> list[dict]:
    """The newest red run per pull request among those finishing in (since, now]."""
    newest: dict = {}
    for raw in data.get("runs") or []:
        run = health.Run(raw)
        if run.pr is None or not run.finished or not (since < run.finished <= now) or not is_red(run):
            continue
        current = newest.get(run.pr)
        if current is None or run.finished > health.Run(current).finished:
            newest[run.pr] = raw
    return [newest[pr] for pr in sorted(newest)]


def elsewhere_and_streak(case: str, red: Red, runs: list[dict]) -> tuple[int, int]:
    """How many other-PR runs in the last day passed `case`, and how many of
    this PR's newest runs in a row (this one included) collapsed it."""
    finish = red.run.finished
    elsewhere = 0
    mine = []
    for raw in runs:
        other = health.Run(raw)
        if not other.finished or not other.full or other.finished > finish:
            continue
        if other.pr == red.run.pr:
            mine.append(other)
        elif finish - ONLY_PR_WINDOW <= other.finished and case in other.passing_cases():
            elsewhere += 1
    streak = 0
    for own in sorted(mine, key=lambda r: r.finished, reverse=True):
        if case in own.collapsed_cases():
            streak += 1
        else:
            break
    return elsewhere, streak


# --------------------------------------------------------------------------- #
# Rendering (GitHub Markdown)
# --------------------------------------------------------------------------- #


def code(case: str) -> str:
    return f"`{case}`"


def join_names(cases: list[dict]) -> str:
    names = [code(c["case"]) for c in cases]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def also_text(count) -> str:
    count = len(count) if isinstance(count, (list, set, tuple)) else int(count or 0)
    return OTHER_PRS.format(n=count, prs=plural(count, "PR")) if count else NO_OTHER_PR


def result_cell(case: dict) -> str:
    reps = case.get("reps") or {}
    total = reps.get("pass", 0) + reps.get("fail", 0) + reps.get("infra", 0)
    cell = f"{reps.get('pass', 0)} / {total} reps"
    if reps.get("infra"):
        cell += f" ({reps['infra']} infra)"
    return cell


def capitalize(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def health_box(red: Red, health_doc: dict, runs: list[dict]) -> str:
    """The paragraph that answers "is it me?"."""
    state = health_doc.get("state")
    incident = state not in (None, health.GREEN)
    since = post_health.parse_iso(health_doc.get("since"))
    links = [LINK_RUN.format(url=RUN_URL.format(build_id=red.run.build_id))]
    if incident:
        links.append(LINK_BRIEF.format(url=post_health.incident_link(health_doc)))
    if red.hard:
        text = HARD_FAILURE
        if red.held_out_failed:
            text += HELD_OUT_NOTE.format(n=len(red.held_out_failed), cases=plural(len(red.held_out_failed), "case"))
        return f"> {text} {' · '.join(links)}"

    theirs, yours, unclear = red.theirs(), red.yours(), red.unclear()
    n = len(red.failed)
    sentences = []
    if incident and theirs:
        if state == health.OUTAGE:
            prs = len((health_doc.get("incident") or {}).get("prs") or [])
            sentences.append(BOX_OUTAGE.format(since=post_health.clock(since, weekday=True), what=capitalize(post_health.describe_cases(health_doc.get("failing_cases"))), prs=prs))
        else:
            sentences.append(BOX_DEGRADED.format(since=post_health.clock(since, weekday=True), cause=post_health.cause_sentence(health_doc)))
        if not yours and not unclear:
            sentences.append(ALL_THEIRS.format(n=n, failures=plural(n, "failure"), are=plural(n, "is", "are"), those=f"those {n}" if n > 1 else "that one"))
        else:
            yours_text = []
            if yours:
                yours_text.append(f"{len(yours)} ({join_names(yours)}) {plural(len(yours), 'looks', 'look')} specific to your PR")
            if unclear:
                yours_text.append(f"{len(unclear)} ({join_names(unclear)}) {plural(len(unclear), 'is', 'are')} unexplained so far")
            sentences.append(
                SOME_THEIRS.format(
                    theirs=len(theirs),
                    n=n,
                    are_theirs=plural(len(theirs), "is", "are"),
                    their_cases=join_names(theirs),
                    yours_text=" and ".join(yours_text),
                    that=plural(len(yours) + len(unclear), "that one", "those"),
                )
            )
    else:
        sentences.append(BOX_HEALTHY if not incident else BOX_DEGRADED.format(since=post_health.clock(since, weekday=True), cause=post_health.cause_sentence(health_doc)))
        for case in theirs:
            sentences.append(SHARED_NO_INCIDENT.format(case=code(case["case"]), also=also_text(case.get("also_failing_prs"))))
        for case in yours:
            elsewhere, streak = elsewhere_and_streak(case["case"], red, runs)
            sentences.append(YOURS_ONLY.format(case=code(case["case"]), elsewhere=elsewhere, runs=plural(elsewhere, "run"), streak=streak))
        if yours:
            sentences.append(LOOKS_YOURS if len(yours) == 1 else LOOK_YOURS)
        for case in unclear:
            sentences.append(UNEXPLAINED.format(case=code(case["case"])))
    return "> " + " ".join(sentences + [" · ".join(links)])


def render_comment(red: Red, health_doc: dict, runs: list[dict]) -> str:
    total = len(red.graded)
    lines = [MARKER, HEADING_HARD if red.hard else HEADING.format(failed=len(red.failed), total=total), "", health_box(red, health_doc, runs), ""]
    rows = red.failed + red.held_out_failed
    if rows:
        lines.append(TABLE_HEAD)
        for case in rows:
            note = "" if case["admitted"] else HELD_OUT_CELL
            lines.append(TABLE_ROW.format(case=case["case"], note=note, result=result_cell(case), also=also_text(case.get("also_failing_prs"))))
        lines.append("")
    reasons = [c for c in red.yours() + red.unclear() if (c.get("excerpt") or c.get("reason"))]
    for case in reasons:
        text = (case.get("excerpt") or case.get("reason") or "").replace("`", "'")[:EXCERPT_CHARS]
        lines.append((REASON_LINE if len(reasons) == 1 else REASON_LINE_NAMED).format(case=case["case"], reason=text))
    if reasons:
        lines.append("")
    project = red.raw.get("project") or UNKNOWN_PROJECT
    project = project.removeprefix(PROJECT_PREFIX)
    wall = red.run.wall_clock
    minutes = int(wall.total_seconds() // 60) if wall else "?"
    passed = red.passed
    lines.append(FOOTER.format(passed=passed, cases=plural(passed, "case"), minutes=minutes, project=project, url=BUILD_LOG_URL.format(pr=red.run.pr, build_id=red.run.build_id)))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Posting
# --------------------------------------------------------------------------- #


def find_comment(gh: ghcli.Gh, pr: int) -> int | None:
    comments = gh.call("GET", gh.path(f"issues/{pr}/comments?per_page=100"), paginate=True)
    for comment in comments or []:
        if isinstance(comment, dict) and str(comment.get("body") or "").startswith(MARKER):
            return comment.get("id")
    return None


def post(gh: ghcli.Gh, pr: int, body: str, known_id: int | None) -> int | None:
    """Edit the marked comment (the one remembered, else the one found), or
    post a new one. Returns the comment id, None when nothing landed."""
    comment_id = known_id or find_comment(gh, pr)
    if comment_id:
        edited = gh.call("PATCH", gh.path(f"issues/comments/{comment_id}"), {"body": body})
        if edited is not None:
            return comment_id
        if known_id:
            # The remembered comment is gone (deleted, or a stale id): the
            # search is the fallback before posting anew.
            found = find_comment(gh, pr)
            if found and found != known_id and gh.call("PATCH", gh.path(f"issues/comments/{found}"), {"body": body}) is not None:
                return found
    if gh.dry_run:
        gh.call("POST", gh.path(f"issues/{pr}/comments"), {"body": body})
        return None
    created = gh.call("POST", gh.path(f"issues/{pr}/comments"), {"body": body})
    return created.get("id") if isinstance(created, dict) else None


# --------------------------------------------------------------------------- #
# One tick
# --------------------------------------------------------------------------- #


def tick(data: dict, health_doc: dict, state: dict | None, now: datetime, roster: health.Roster, gh: ghcli.Gh) -> tuple[dict, list[tuple[int, str, str]]]:
    """Returns (new state, [(pr, build_id, body)] rendered this tick)."""
    before = state or {}
    since = post_health.parse_iso(before.get("last_comment_tick")) or (now - FIRST_TICK_LOOKBACK)
    comments = dict(before.get("comments") or {})
    runs = [r for r in data.get("runs") or [] if isinstance(r, dict)]
    rendered = []
    retry_from = None
    for raw in newest_red_per_pr(data, since, now):
        run = health.Run(raw)
        key = str(run.pr)
        if comments.get(key, {}).get("build_id") == run.build_id:
            continue  # never twice for the same build
        admitted = roster.at(run.started or run.finished)
        verdict = classify.classify_run(raw, runs, health_doc, now, admitted=admitted)
        red = Red(raw, verdict, admitted)
        body = render_comment(red, health_doc, runs)
        rendered.append((run.pr, run.build_id, body))
        known = comments.get(key, {}).get("comment_id")
        comment_id = post(gh, run.pr, body, known)
        if comment_id is None and not gh.dry_run:
            log(f"warning: no comment landed on #{run.pr} for build {run.build_id}; retried next tick")
            retry_from = min(retry_from or run.finished, run.finished)
            continue
        comments[key] = {"comment_id": comment_id or known, "build_id": run.build_id, "at": health.iso(now)}
    cutoff = now - STATE_RETENTION
    comments = {pr: entry for pr, entry in comments.items() if (post_health.parse_iso(entry.get("at")) or now) >= cutoff}
    watermark = retry_from - RETRY_BACKOFF if retry_from else now
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_comment_tick": health.iso(watermark),
        "comments": comments,
        "updated_at": health.iso(now),
    }, rendered


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=pathlib.Path, required=True, help="data.json (schema v1)")
    parser.add_argument("--health", type=pathlib.Path, required=True, help="the health.json health.py wrote this tick")
    parser.add_argument("--state", required=True, help="this script's state: local path or gs:// object")
    parser.add_argument("--repo", default=ghcli.DEFAULT_REPO, help="owner/repo the pull requests live in")
    parser.add_argument("--now", type=health.parse_when, help="evaluate as of this ISO 8601 time (default: now)")
    parser.add_argument("--admitted", help="comma-separated admitted roster (default: BOOTSTRAP_ADMITTED in hack/ci-eval-pr.sh)")
    parser.add_argument("--ci-eval-script", type=pathlib.Path, default=health.CI_EVAL_SCRIPT, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="print the comments instead of posting; still updates --state")
    return parser.parse_args(argv)


def main(argv=None, runner=subprocess.run, gh_runner=None) -> int:
    args = parse_args(argv)
    data = health.load_json(args.data)
    health_doc = health.load_json(args.health)
    if data is None or health_doc is None:
        log(f"ERROR: {args.data} and {args.health} must both be readable JSON objects")
        return 1
    roster = health.Roster.fixed(name for name in args.admitted.split(",") if name) if args.admitted else health.Roster.from_script(args.ci_eval_script)
    now = args.now or datetime.now(UTC)
    gh = ghcli.Gh(args.repo, gh_runner or runner, dry_run=args.dry_run)
    state = post_health.read_state(args.state, runner)
    new_state, rendered = tick(data, health_doc, state, now, roster, gh)
    for pr, build_id, body in rendered:
        if args.dry_run:
            log(f"--dry-run: would comment on #{pr} (build {build_id})\n{body}")
    post_health.write_state(args.state, new_state, runner)
    log(f"gate comments: {len(rendered)} red {plural(len(rendered), 'run')} since {state.get('last_comment_tick') if state else 'the first tick'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
