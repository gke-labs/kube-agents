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


SETTINGS_PATH = "/opt/data/SETTINGS.md"

# The only directory a report may be read from. The report is posted publicly
# and then unlinked, so the path is confined rather than merely existence-checked.
SCRATCH_DIR = "/opt/data/scratch"

# Shell convention for "command not found", reused so a missing binary stays
# distinguishable from a gh command that ran and failed.
GH_MISSING_RC = 127

# The operator writes this literal when no GitOps repo is configured
# (buildSettingsConfigMap in platformagent_manifests.go). It means "absent",
# not "malformed".
SETTINGS_REPO_UNSET = "none"

# The host must sit at the *start* of the value, after an optional scheme and
# optional userinfo — not merely after some delimiter. Both spellings reject
# "https://evilgithub.com/attacker/repo", but only this one rejects github.com
# appearing as a path segment on another host, which is how
# "https://evil.com/github.com/attacker/repo" resolved to "attacker/repo".
#
# The scheme alternation mirrors the four ValidateGitRepoURL accepts
# (common_types.go). Excluding "/" from the userinfo class is what stops
# "https://user@evil.com/github.com/a/b". The trailing "[/:]" accepts both web
# URLs and SCP-form SSH remotes ("git@github.com:acme/toolkit.git"); the
# optional "www." preserves the prefix the original parser handled explicitly.
#
# urllib.parse is not usable here: SCP-form remotes have no valid scheme (the
# "@" disqualifies "git@github.com" as one, so the whole string comes back as
# a path), and the bare "owner/repo" shorthand below has no host at all.
REPO_URL_RE = re.compile(
    r"^(?:(?:https?|git|ssh)://)?(?:[^/@]+@)?(?:www\.)?github\.com[/:]"
    r"([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)"
)

# The operator accepts a bare "owner/repo" shorthand as a valid gitRepo and
# writes it through to SETTINGS.md verbatim, so it reaches us hostless. This
# mirrors ownerRepoRegex in k8s-operator/api/v1alpha1/common_types.go, which is
# the contract for what can land in the file — treating the shorthand as
# malformed would alert on a supported configuration. It is also the form
# `gh -R` takes natively.
BARE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RepoUnparseable(Exception):
    """SETTINGS.md names a target repository, but it could not be understood.

    Distinct from *absent* on purpose. An operator who configured nothing has
    nothing for us to do — that is silence. An operator who configured
    something we cannot read is a fault, and silence there means the resolver
    stops working and nobody finds out.
    """


def _valid_repo_component(part: str) -> bool:
    """Reject path components that are unsafe to hand to ``gh -R``.

    The regex character class permits "." and "-", so it happily produces
    "../..", and a leading dash would be parsed by ``gh`` as a flag rather
    than as part of the repository slug. Neither is a shape problem the
    pattern can express, so it is checked here.
    """
    return bool(part) and part not in (".", "..") and not part.startswith("-")


def get_target_repo(
    required: bool = True, settings_path: Optional[str] = None
) -> Optional[str]:
    """Extract the target repository slug from SETTINGS.md.

    Returns ``owner/repo``. Raises :class:`RepoUnparseable` when a repository
    is configured but cannot be parsed. When none is configured at all, obeys
    ``required``: exit for callers that cannot proceed without one, return
    ``None`` for callers that treat it as "nothing to do".
    """
    # Resolved at call time, not bound as a default, so the module constant
    # stays the single source of truth (and is patchable under test).
    settings_path = settings_path or SETTINGS_PATH
    if not os.path.exists(settings_path):
        if required:
            print(f"Error: {settings_path} not found.", file=sys.stderr)
            sys.exit(1)
        return None

    configured = None
    with open(settings_path, "r", encoding="utf-8") as f:
        for line in f:
            if "Git Repo:" in line:
                configured = line.split("Git Repo:", 1)[1]
                # Strip the markdown bold delimiters the operator emits
                # around the key, leaving just the value.
                configured = configured.replace("*", "").strip()
                break

    if (
        configured is None
        or not configured
        or configured.lower() == SETTINGS_REPO_UNSET
    ):
        if required:
            print(
                f"Error: No target repository configured in {settings_path}.",
                file=sys.stderr,
            )
            sys.exit(1)
        return None

    match = REPO_URL_RE.search(configured)
    if match:
        repo = match.group(1)
    elif BARE_REPO_RE.match(configured):
        repo = configured
    else:
        raise RepoUnparseable(configured)

    repo = re.sub(r"\.git$", "", repo)
    owner, _, name = repo.partition("/")
    # Checked after the shorthand branch, not instead of it: "../.." satisfies
    # BARE_REPO_RE, so the component guard is what rejects it.
    if not _valid_repo_component(owner) or not _valid_repo_component(name):
        raise RepoUnparseable(configured)

    return repo


def resolve_repo_or_exit(required: bool = True) -> Optional[str]:
    """``get_target_repo`` for callers that cannot route the fault themselves.

    ``claim`` and ``transition`` are invoked with an issue number already in
    hand, so there is no useful degraded mode: a repository we cannot parse is
    a hard stop, exactly as it was before the value became optional.
    """
    try:
        return get_target_repo(required=required)
    except RepoUnparseable as e:
        print(
            f"Error: Could not extract target repository from {SETTINGS_PATH}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)


def run_gh(args: list, check: bool = True) -> subprocess.CompletedProcess:
    """Runs a gh CLI command safely without shell escaping or ampersand backgrounding issues."""
    try:
        return subprocess.run(
            ["gh"] + args, check=check, text=True, capture_output=True
        )
    except FileNotFoundError:
        if check:
            print("Error: 'gh' CLI binary not found in PATH.", file=sys.stderr)
            sys.exit(GH_MISSING_RC)
        # check=False callers want to degrade gracefully, not die here. The
        # code is distinguishable so callers can name the fault precisely.
        return subprocess.CompletedProcess(
            ["gh"] + args,
            GH_MISSING_RC,
            stdout="",
            stderr="'gh' CLI binary not found in PATH.",
        )
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
    # Nothing configured is not a fault: it is a supported deployment with no
    # work to do. Its own status, rather than NO_ISSUES, so the two cannot be
    # confused by a later reader.
    try:
        repo = get_target_repo(required=False)
    except RepoUnparseable as e:
        print(
            json.dumps(
                {"status": "ERROR", "reason": "GIT_REPO_UNPARSEABLE", "value": str(e)}
            )
        )
        return
    if not repo:
        print(json.dumps({"status": "NOT_CONFIGURED"}))
        return
    # Check auth pre-flight safely. A repo is configured but credentials are
    # broken: that is a real fault, so it must NOT be reported as NO_ISSUES
    # (which the skill silences) or the resolver goes quiet forever.
    auth = run_gh(["auth", "status"], check=False)
    if auth.returncode != 0:
        # An absent binary and a rejected token need different operators and
        # different fixes, so they do not share a reason code.
        reason = (
            "GH_CLI_NOT_FOUND"
            if auth.returncode == GH_MISSING_RC
            else "GITHUB_AUTH_NOT_CONFIGURED"
        )
        print(json.dumps({"status": "ERROR", "reason": reason}))
        return

    # Sweep stale issues first
    sweep_stale_issues(repo)

    # Query next unaddressed issue.
    # `agent:audit` is excluded because those issues are fleet-audit ledgers:
    # that skill owns them and rewrites them in place on every run.
    search_query = "is:issue is:open -label:status:in-progress -label:status:escalation-needed -label:agent:ignore -label:status:resolved -label:agent:audit"
    # check=False: `gh auth status` passes when *any* host is authenticated, so
    # a token without scope for this repo — or a repo that 404s — only fails
    # here. With check=True that exits non-zero having printed no JSON at all,
    # which the skill has no branch for.
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
        ],
        check=False,
    )
    if res.returncode != 0:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason": "REPO_UNREACHABLE",
                    "repository": repo,
                }
            )
        )
        return

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
    repo = resolve_repo_or_exit(required=True)
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
    repo = resolve_repo_or_exit(required=True)
    issue_num = str(args.issue)
    state = args.state
    report_file = args.report_file

    # Prevent Path Traversal & Arbitrary File Deletion. The report is posted
    # publicly and then unlinked, so anything resolving outside the scratch
    # directory — including via symlink — is rejected outright.
    scratch_dir = os.path.realpath(SCRATCH_DIR)
    real_report_path = os.path.realpath(report_file)
    if not real_report_path.startswith(scratch_dir + os.sep):
        print(
            f"Error: Report file {report_file} resolves outside {scratch_dir}.",
            file=sys.stderr,
        )
        sys.exit(1)
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
