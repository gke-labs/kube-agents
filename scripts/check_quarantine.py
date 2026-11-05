#!/usr/bin/env python3
"""List the quarantined tests and check that each one is still owed its skip.

A quarantined test is a Go or Python test skipped in its own source because it
flakes, with the skip reason naming the issue that tracks the real fix:

    Go:      if os.Getenv("KA_RUN_QUARANTINED") == "" {
                 t.Skip("quarantined: gke-labs/kube-agents#1234")
             }
    Python:  @unittest.skipUnless(os.environ.get("KA_RUN_QUARANTINED"),
                                  "quarantined: gke-labs/kube-agents#1234")

`docs/pull-request-workflow.md` ("Quarantining a flaky test") holds the policy:
when to quarantine, what the issue has to say, which tests are never skipped
without a maintainer's call. This script holds the checks.

Offline (the default, and what `scripts/test_check_quarantine.py` runs on every
pull request): every quarantine string in the tree cites an issue number and
sits beside a `KA_RUN_QUARANTINED` check, so whoever fixes the flake can run
the test without editing it first.

`--github` (the daily `.github/workflows/quarantine-check.yml`) reads each
cited issue and fails when one is closed (the fix landed and the skip stayed),
lacks the `ci:quarantined` label, has no assignee to own the fix, or never
states a suspected cause and that a product race was ruled out. A flake that
was really a product race is a bug, and skipping its test hides it. A test
under `PROTECTED_PREFIXES` (conformance, auth-callout) also needs a
`Quarantine approved` comment from one of `APPROVERS`: a flaky security test is
fixed, not skipped, unless they say otherwise. The script also reports how many
tests are quarantined and for how long, flags any quarantined longer than
`STALE_DAYS`, and flags any open `ci:quarantined` issue whose skip is no longer
in the tree.

`--notify` tells the people who can act: it comments on each issue with a
problem, naming its assignees, once per issue per kind of problem (closed,
incomplete, stale), so a standing problem is not re-told every day. It creates
the label the first time it needs one.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from github_api import GitHubAPI, log  # noqa: E402

REPO = "gke-labs/kube-agents"
REPO_ROOT = _HERE.parent
#: The skip reason every quarantine carries, and the one thing this script greps for.
MARKER_RE = re.compile(r"quarantined: gke-labs/kube-agents#(\d+)")
#: Any `quarantined: ...#` string; one that MARKER_RE does not match is malformed.
LOOSE_MARKER_RE = re.compile(r"quarantined:\s*\S*#")
#: The variable that runs quarantined tests anyway.
RUN_ENV = "KA_RUN_QUARANTINED"
#: The read of `RUN_ENV` that counts as a gate: Go `Getenv("...")`, Python `environ.get("...")` or
#: `getenv("...")`. A bare mention (a comment) does not.
GATE_RE = re.compile(r"""(?:Getenv|environ\.get|getenv)\(\s*["']""" + RUN_ENV + r"""["']""")
#: How many lines above the marker may hold the `RUN_ENV` check (the Go `if` sits one above
#: `t.Skip`; a Python decorator split over lines puts it one above the reason).
GATE_WINDOW = 2
SOURCE_GLOBS = ("*.go", "*.py")
#: This script and its tests quote the marker in prose and fixtures.
SELF_FILES = frozenset({"scripts/check_quarantine.py", "scripts/test_check_quarantine.py"})
LABEL = "ci:quarantined"
LABEL_COLOR = "fbca04"
LABEL_DESCRIPTION = "A test is skipped in source until this issue is fixed (scripts/check_quarantine.py)"
#: Tests that are fixed, not skipped: a quarantine under one of these needs an `APPROVERS` comment
#: on its issue carrying `APPROVAL_RE`.
PROTECTED_PREFIXES = ("tests/conformance/", "a2a/authcallout/")
APPROVERS = frozenset({"bnaylor"})
APPROVAL_RE = re.compile(r"^\s*quarantine approved\b", re.IGNORECASE | re.MULTILINE)
#: Opens each comment the daily run posts; one per issue per kind, so a standing problem is told once.
COMMENT_MARKER = "<!-- quarantine-check:{kind} -->"
KIND_CLOSED = "closed"
KIND_INCOMPLETE = "incomplete"
KIND_STALE = "stale"
#: A quarantine older than this is flagged in the report.
STALE_DAYS = 14
#: Lines the tracking issue (body or a comment) must carry, matched at the start of a line.
CAUSE_RE = re.compile(r"^\s*suspected cause:\s*\S", re.IGNORECASE | re.MULTILINE)
RACE_RE = re.compile(r"^\s*product race ruled out:\s*yes\b", re.IGNORECASE | re.MULTILINE)
USER_AGENT = "kube-agents-check-quarantine"
TIMELINE_LABELED = "labeled"
SECONDS_PER_DAY = 86400


@dataclass
class Quarantine:
    path: str
    line: int
    issue: int


@dataclass
class Report:
    quarantines: list[Quarantine] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: issue number -> days since the label went on, when known
    ages: dict[int, int] = field(default_factory=dict)
    #: issue number -> kind -> the problems to tell its assignees about
    notices: dict[int, dict[str, list[str]]] = field(default_factory=dict)
    #: issue number -> the assignees and comments read, for `notify`
    issues: dict[int, dict] = field(default_factory=dict)

    def fail(self, number: int, kind: str, text: str) -> None:
        self.failures.append(text)
        self.notices.setdefault(number, {}).setdefault(kind, []).append(text)

    def warn(self, number: int, kind: str, text: str) -> None:
        self.warnings.append(text)
        self.notices.setdefault(number, {}).setdefault(kind, []).append(text)


def source_files(root: Path = REPO_ROOT) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", *SOURCE_GLOBS], cwd=root, check=True, capture_output=True, text=True
    ).stdout
    return [p for p in out.splitlines() if p and p not in SELF_FILES]


def scan_text(path: str, text: str, report: Report) -> None:
    """Record each quarantine in one file, and fail any that is malformed or ungated."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = MARKER_RE.search(line)
        if not match:
            if LOOSE_MARKER_RE.search(line):
                report.failures.append(
                    f"{path}:{index + 1}: malformed quarantine string; write `quarantined: {REPO}#<issue>`"
                )
            continue
        report.quarantines.append(Quarantine(path, index + 1, int(match.group(1))))
        window = lines[max(0, index - GATE_WINDOW) : index + 1]
        if not any(GATE_RE.search(candidate) for candidate in window):
            report.failures.append(
                f"{path}:{index + 1}: quarantine of #{match.group(1)} does not check {RUN_ENV}, "
                "so nobody can run the test without editing it"
            )


def scan_tree(root: Path = REPO_ROOT, report: Report | None = None) -> Report:
    report = report or Report()
    for path in source_files(root):
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        scan_text(path, text, report)
    return report


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def labeled_at(api, number: int) -> datetime | None:
    """When `LABEL` last went on the issue, from its timeline."""
    when = None
    for event in api.get_all(f"/repos/{api.repo}/issues/{number}/timeline"):
        if event.get("event") == TIMELINE_LABELED and (event.get("label") or {}).get("name") == LABEL:
            when = parse_time(event["created_at"])
    return when


def check_issue(api, number: int, sites: list[Quarantine], report: Report, now: datetime) -> None:
    where = ", ".join(f"{q.path}:{q.line}" for q in sites)
    issue = api.get(f"/repos/{api.repo}/issues/{number}")
    comments = api.get_all(f"/repos/{api.repo}/issues/{number}/comments")
    report.issues[number] = {"assignees": issue.get("assignees") or [], "comments": comments}
    if issue.get("state") == "closed":
        report.fail(number, KIND_CLOSED, f"#{number} is closed but still quarantines {where}; remove the skip")
        return
    labels = {label.get("name") for label in issue.get("labels") or []}
    if LABEL not in labels:
        report.fail(number, KIND_INCOMPLETE, f"#{number} quarantines {where} but lacks the `{LABEL}` label")
    if not issue.get("assignees"):
        report.fail(number, KIND_INCOMPLETE, f"#{number} quarantines {where} but has no assignee to own the fix")
    texts = [issue.get("body") or ""] + [c.get("body") or "" for c in comments]
    if not any(CAUSE_RE.search(t) for t in texts):
        report.fail(number, KIND_INCOMPLETE, f"#{number} never states `Suspected cause:`; a quarantine needs one")
    if not any(RACE_RE.search(t) for t in texts):
        report.fail(
            number,
            KIND_INCOMPLETE,
            f"#{number} never states `Product race ruled out: yes`; until it is, the test stays live",
        )
    protected = [q for q in sites if q.path.startswith(PROTECTED_PREFIXES)]
    approved = any(
        (c.get("user") or {}).get("login") in APPROVERS and APPROVAL_RE.search(c.get("body") or "")
        for c in comments
    )
    if protected and not approved:
        report.fail(
            number,
            KIND_INCOMPLETE,
            f"#{number} quarantines a security or conformance test "
            f"({', '.join(f'{q.path}:{q.line}' for q in protected)}) without a `Quarantine approved` "
            f"comment from {', '.join(sorted(APPROVERS))}",
        )
    since = labeled_at(api, number) if LABEL in labels else None
    if since is not None:
        days = int((now - since).total_seconds() // SECONDS_PER_DAY)
        report.ages[number] = days
        if days > STALE_DAYS:
            report.warn(
                number, KIND_STALE, f"#{number} has quarantined {where} for {days} days (over {STALE_DAYS})"
            )


def check_github(api, report: Report, now: datetime) -> Report:
    by_issue: dict[int, list[Quarantine]] = {}
    for quarantine in report.quarantines:
        by_issue.setdefault(quarantine.issue, []).append(quarantine)
    for number in sorted(by_issue):
        check_issue(api, number, by_issue[number], report, now)
    for issue in api.get_all(f"/repos/{api.repo}/issues?state=open&labels={LABEL}"):
        if "pull_request" in issue:
            continue
        if issue["number"] not in by_issue:
            report.warnings.append(
                f"#{issue['number']} is labelled `{LABEL}` but no test cites it; "
                "remove the label or restore the skip"
            )
    return report


def notify(api, report: Report) -> int:
    """Comment once per issue per kind of problem, naming its assignees. Returns comments posted."""
    posted = 0
    if report.notices:
        api.post(
            f"/repos/{api.repo}/labels",
            {"name": LABEL, "color": LABEL_COLOR, "description": LABEL_DESCRIPTION},
            tolerate=(422,),
        )
    for number, kinds in sorted(report.notices.items()):
        read = report.issues.get(number, {})
        bodies = [c.get("body") or "" for c in read.get("comments", [])]
        owners = " ".join(f"@{a['login']}" for a in read.get("assignees", []) if a.get("login"))
        for kind, items in sorted(kinds.items()):
            marker = COMMENT_MARKER.format(kind=kind)
            if any(marker in body for body in bodies):
                continue
            lines = [marker, f"{owners}".strip() or "Unassigned.", "", "The daily quarantine check found:", ""]
            lines += [f"- {item}" for item in items]
            lines += ["", "Policy: `docs/pull-request-workflow.md`, \"Quarantining a flaky test\"."]
            api.post(f"/repos/{api.repo}/issues/{number}/comments", {"body": "\n".join(lines)})
            posted += 1
    return posted


def render(report: Report, online: bool) -> str:
    issues = sorted({q.issue for q in report.quarantines})
    lines = ["## Quarantined tests", ""]
    summary = f"{len(report.quarantines)} quarantined test(s) across {len(issues)} issue(s)"
    if report.ages:
        summary += f"; oldest {max(report.ages.values())} days"
        stale = sum(1 for days in report.ages.values() if days > STALE_DAYS)
        summary += f"; {stale} over {STALE_DAYS} days"
    lines += [summary + ".", ""]
    if report.quarantines:
        lines += ["| Issue | Age (days) | Test |", "| --- | --- | --- |"]
        for q in sorted(report.quarantines, key=lambda q: (q.issue, q.path, q.line)):
            age = report.ages.get(q.issue, "?" if online else "-")
            lines.append(f"| #{q.issue} | {age} | `{q.path}:{q.line}` |")
        lines.append("")
    for heading, items in (("Failures", report.failures), ("Warnings", report.warnings)):
        if items:
            lines += [f"### {heading}", ""] + [f"- {item}" for item in items] + [""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--github", action="store_true", help="also read each cited issue (needs GITHUB_TOKEN)")
    parser.add_argument("--repo", default=REPO)
    parser.add_argument("--notify", action="store_true", help="with --github, comment on each issue with a problem")
    args = parser.parse_args(argv)

    report = scan_tree()
    if args.github:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            log("GITHUB_TOKEN is not set")
            return 2
        api = GitHubAPI(args.repo, token, user_agent=USER_AGENT)
        check_github(api, report, datetime.now(timezone.utc))
        if args.notify:
            log(f"posted {notify(api, report)} comment(s)")

    text = render(report, args.github)
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    for warning in report.warnings:
        print(f"::warning::{warning}")
    for failure in report.failures:
        print(f"::error::{failure}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
