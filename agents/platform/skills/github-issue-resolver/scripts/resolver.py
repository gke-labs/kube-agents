#!/usr/bin/env python3
"""
resolver.py — Deterministic helper script for the github-issue-resolver skill.
Encapsulates GitHub CLI (gh) operations, label management, stale issue sweeps,
and safe report uploading via standard subprocess execution.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from typing import Optional


def get_target_repo(required: bool = False) -> Optional[str]:
    """Extracts target repository from /opt/data/SETTINGS.md."""
    settings_path = "/opt/data/SETTINGS.md"
    if not os.path.exists(settings_path):
        if required:
            print(f"Error: {settings_path} not found.", file=sys.stderr)
            sys.exit(1)
        return None

    with open(settings_path, "r", encoding="utf-8") as f:
        for line in f:
            if "Git Repo:" in line:
                match = re.search(
                    r"github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", line
                )
                if match:
                    repo = match.group(1).rstrip(".git")
                    if re.match(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", repo):
                        return repo

    if required:
        print(
            "Error: Could not extract target repository from /opt/data/SETTINGS.md.",
            file=sys.stderr,
        )
        sys.exit(1)
    return None


def run_gh(args: list, check: bool = True) -> subprocess.CompletedProcess:
    """Runs a gh CLI command safely without shell escaping or ampersand backgrounding issues."""
    try:
        return subprocess.run(
            ["gh"] + args, check=check, text=True, capture_output=True
        )
    except FileNotFoundError:
        print("Error: 'gh' CLI binary not found in PATH.", file=sys.stderr)
        sys.exit(127)
    except subprocess.CalledProcessError as e:
        if check:
            print(
                f"Error running gh command: {' '.join(args)}\n{e.stderr}",
                file=sys.stderr,
            )
            sys.exit(e.returncode)
        return e


def ensure_labels_exist(repo: str):
    """Ensures required status and governance labels exist on the repository."""
    labels = [
        (
            "status:in-progress",
            "FBCA04",
            "Currently being actively investigated by the Platform Agent",
        ),
        (
            "status:resolved",
            "0E8A16",
            "Issue resolved autonomously by Platform Agent",
        ),
        (
            "status:escalation-needed",
            "B60205",
            "Issue requires human review/SRE action",
        ),
        (
            "agent:ignore",
            "E99695",
            "Permanently ignored by automated issue resolvers",
        ),
    ]
    for name, color, desc in labels:
        run_gh(
            [
                "label",
                "create",
                name,
                "-R",
                repo,
                "--color",
                color,
                "--description",
                desc,
                "--force",
            ],
            check=False,
        )


def sweep_stale_issues(repo: str):
    """Detects issues labeled status:in-progress untouched for >2 hours, transitions and alerts."""
    res = run_gh(
        [
            "issue",
            "list",
            "-R",
            repo,
            "--label",
            "status:in-progress",
            "--json",
            "number,title,updatedAt",
        ],
        check=False,
    )
    if res.returncode != 0:
        return

    try:
        issues = json.loads(res.stdout)
        if not isinstance(issues, list):
            issues = []
    except Exception:
        issues = []

    now = datetime.datetime.now(datetime.timezone.utc)
    stale_msg = (
        "🚨 **Autonomous Investigation Timed Out — Human Escalation Required**\n\n"
        "The Platform Agent previously claimed this issue (`status:in-progress`) but no updates were "
        "recorded within the 2-hour SLA window (stale investigation/crash). Transitioning to human review."
    )

    for i in issues:
        updated_str = i.get("updatedAt")
        if not updated_str:
            continue
        try:
            updated = datetime.datetime.fromisoformat(
                updated_str.replace("Z", "+00:00")
            )
            if (now - updated).total_seconds() > 7200:
                num = str(i["number"])
                # Post timeout comment and transition label
                run_gh(
                    [
                        "issue",
                        "comment",
                        num,
                        "-R",
                        repo,
                        "--body",
                        stale_msg,
                    ],
                    check=False,
                )
                run_gh(
                    [
                        "issue",
                        "edit",
                        num,
                        "-R",
                        repo,
                        "--add-label",
                        "status:escalation-needed",
                        "--remove-label",
                        "status:in-progress",
                    ],
                    check=False,
                )
        except Exception:
            continue


def handle_poll(args):
    repo = get_target_repo(required=False)
    if not repo:
        print(json.dumps({"status": "NO_ISSUES", "reason": "NO_REPO_CONFIGURED"}))
        return
    # Check auth pre-flight safely
    auth = run_gh(["auth", "status"], check=False)
    if auth.returncode != 0:
        print(
            json.dumps(
                {"status": "NO_ISSUES", "reason": "GITHUB_AUTH_NOT_CONFIGURED"}
            )
        )
        return

    # Sweep stale issues first
    sweep_stale_issues(repo)

    # Query next unaddressed issue
    search_query = "is:issue is:open -label:status:in-progress -label:status:escalation-needed -label:agent:ignore -label:status:resolved"
    res = run_gh(
        [
            "issue",
            "list",
            "-R",
            repo,
            "--search",
            search_query,
            "--json",
            "number,title,body,comments",
            "--limit",
            "10",
        ]
    )

    try:
        issues = json.loads(res.stdout)
        if not isinstance(issues, list):
            issues = []
    except Exception:
        issues = []

    if not issues:
        print(json.dumps({"status": "NO_ISSUES", "repository": repo}))
        return

    # Select lowest numbered open issue
    issues.sort(key=lambda x: int(x["number"]))
    target = issues[0]
    comments = []
    for c in target.get("comments", []):
        author = c.get("author", {}).get("login", "unknown")
        body = c.get("body", "")
        created = c.get("createdAt", "")
        comments.append({"author": author, "createdAt": created, "body": body})

    print(
        json.dumps(
            {
                "status": "FOUND",
                "repository": repo,
                "issue_number": target["number"],
                "title": target["title"],
                "body": target.get("body", ""),
                "comments": comments,
            },
            indent=2,
        )
    )


def handle_claim(args):
    repo = get_target_repo(required=True)
    issue_num = str(args.issue)
    ensure_labels_exist(repo)

    run_gh(
        [
            "issue",
            "edit",
            issue_num,
            "-R",
            repo,
            "--add-label",
            "status:in-progress",
        ]
    )
    claim_msg = (
        "🤖 **Platform Agent Triaging:** Issue marked `status:in-progress`. "
        "Beginning root cause investigation and recording worklog..."
    )
    run_gh(
        [
            "issue",
            "comment",
            issue_num,
            "-R",
            repo,
            "--body",
            claim_msg,
        ]
    )

    print(
        json.dumps(
            {
                "status": "CLAIMED",
                "issue_number": int(issue_num),
                "repository": repo,
            },
            indent=2,
        )
    )


def handle_transition(args):
    repo = get_target_repo(required=True)
    issue_num = str(args.issue)
    state = args.state
    report_file = args.report_file

    # Prevent Path Traversal & Arbitrary File Deletion
    scratch_dir = os.path.realpath("/opt/data/scratch")
    real_report_path = os.path.realpath(report_file)
    if not real_report_path.startswith(
        scratch_dir + os.sep
    ) and real_report_path != os.path.join(scratch_dir, os.path.basename(report_file)):
        if not os.path.exists(real_report_path):
            print(
                f"Error: Report file {report_file} does not exist.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Post report comment directly via file parameter (-F)
    run_gh(["issue", "comment", issue_num, "-R", repo, "-F", real_report_path])

    # Transition label
    run_gh(
        [
            "issue",
            "edit",
            issue_num,
            "-R",
            repo,
            "--add-label",
            f"status:{state}",
            "--remove-label",
            "status:in-progress",
        ]
    )

    # If resolved, close the issue
    if state == "resolved":
        run_gh(
            [
                "issue",
                "close",
                issue_num,
                "-R",
                repo,
                "--reason",
                "completed",
            ]
        )

    # Cleanup temporary report file
    try:
        os.remove(real_report_path)
    except Exception:
        pass

    print(
        json.dumps(
            {
                "status": "TRANSITIONED",
                "issue_number": int(issue_num),
                "new_state": state,
                "repository": repo,
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description="Deterministic GitHub issue resolver helper."
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # poll
    subparsers.add_parser(
        "poll", help="Poll unaddressed issues and sweep stale investigations."
    )

    # claim
    claim_parser = subparsers.add_parser("claim", help="Claim an open issue.")
    claim_parser.add_argument(
        "--issue", required=True, type=int, help="Issue number to claim."
    )

    # transition
    trans_parser = subparsers.add_parser(
        "transition", help="Upload report and transition issue label/state."
    )
    trans_parser.add_argument(
        "--issue", required=True, type=int, help="Issue number to transition."
    )
    trans_parser.add_argument(
        "--state",
        required=True,
        choices=["resolved", "escalation-needed"],
        help="New state label.",
    )
    trans_parser.add_argument(
        "--report-file",
        required=True,
        help="Path to markdown report file to post as comment.",
    )

    args = parser.parse_args()
    if args.subcommand == "poll":
        handle_poll(args)
    elif args.subcommand == "claim":
        handle_claim(args)
    elif args.subcommand == "transition":
        handle_transition(args)


if __name__ == "__main__":
    main()
