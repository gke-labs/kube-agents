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
from pathlib import Path

# The shared scripts dir holds github_token_refresh (docker-entrypoint.sh keeps
# executable scripts shared across profiles rather than copying them into each
# one). The third entry is the same directory in a source checkout. Mirrors
# fleet-audit's audit_report, which needs the same module for the same reason.
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

# `sandbox_exec` has no import-time dependency on anything under /opt — the yaml
# it needs to read the managed config is imported inside the function that reads
# it, and a missing config just means "no sandbox".
import sandbox_exec  # noqa: E402 — needs the sys.path lines above
from github_token_refresh import (  # noqa: E402 — needs the sys.path lines above
    GH_MISSING_RC,
    is_refresh_failed,
    looks_like_auth_failure,
    refresh_credentials_once,
)
from gitops_workspace import (  # noqa: E402 — needs the sys.path lines above
    get_managed_github_repos,
    is_valid_repo_slug,
)

SCRATCH_DIR = "/opt/data/scratch"

# The value `gh` reads as "this input file is stdin". `forge.BODY_STDIN` and
# `audit_report.BODY_STDIN` spell the same thing for the same reason; the shim
# forwards fd 0 only for a `STDIN_FILE_FLAGS` flag whose value is exactly this.
BODY_STDIN = "-"


# The operator accepts a bare "owner/repo" shorthand as a valid gitRepo and
# writes it through to SETTINGS.md verbatim, so it reaches us hostless. This
# mirrors ownerRepoRegex in k8s-operator/api/v1alpha1/common_types.go, which is
# the contract for what can land in the file — treating the shorthand as
# malformed would alert on a supported configuration. It is also the form
# `gh -R` takes natively.
BARE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _run_gh_once(args: list, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run one gh command, mapping a missing binary onto a return code.

    Never raises for a non-zero exit, so :func:`run_gh` can inspect a failure
    and decide whether it is worth retrying before applying the caller's
    ``check`` semantics.

    Routed through ``sandbox_exec`` because this file runs on both sides of the
    sandbox boundary. ``poll`` is a subprocess of ``github_scan_gate.py`` in the
    agent pod, which as of #737 has no ``gh``; ``claim``, ``transition`` and the
    report subcommands are invoked by the model, from a shell that is already in
    the sandbox. ``sandbox_exec.run`` forwards over ssh in the first case and
    falls back to a local subprocess in the second, so one call site serves
    both.

    ``SandboxUnavailable`` is left to propagate. A poll that cannot reach the
    sandbox has not learned that the repository is quiet, and reporting it as
    quiet is the outcome this path exists to avoid.

    ``stdin`` carries a document to a command that named ``-`` as its input
    file. It is a string rather than a handle precisely so the retry in
    :func:`run_gh` can send it twice; a stream would be empty the second time.
    """
    try:
        return sandbox_exec.run(["gh"] + args, stdin=stdin)
    except FileNotFoundError:
        # Distinguishable from a gh command that ran and failed, so callers can
        # name the fault precisely.
        return subprocess.CompletedProcess(
            ["gh"] + args,
            GH_MISSING_RC,
            stdout="",
            stderr="'gh' CLI binary not found in PATH.",
        )


def run_gh(args: list, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Runs a gh CLI command safely without shell escaping or ampersand backgrounding issues.

    A failed call gets one retry behind a freshly minted token. The credential
    is a GitHub App installation token with a one-hour life, and nothing else
    on this path re-mints it, so an expired token is the *expected* steady
    state between refreshes rather than an exceptional one.

    Retrying here rather than at each call site is what keeps `claim` and
    `transition` alive: they run in separate invocations, long after the `poll`
    that filed their card, and an expiry between the claim and the report used
    to exit before the report was posted -- losing the investigation and
    leaving the issue pinned at `status:in-progress` until the stale sweep
    escalated it.

    Only an *authentication* failure earns the retry. ``looks_like_auth_failure``
    owns that judgement, including why a missing binary and a sidecar timeout are
    excluded: no token puts an absent binary back on PATH, and a 404, a rate
    limit, or a timeout is not a credential problem either.
    """
    result = _run_gh_once(args, stdin)
    if looks_like_auth_failure(args, result) and refresh_credentials_once(args):
        result = _run_gh_once(args, stdin)

    if check and result.returncode != 0:
        if result.returncode == GH_MISSING_RC:
            print("Error: 'gh' CLI binary not found in PATH.", file=sys.stderr)
        else:
            print(
                f"Error running gh command: {' '.join(args)}\n{result.stderr}",
                file=sys.stderr,
            )
        sys.exit(result.returncode)
    return result


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


def _is_safe_char(ch: str) -> bool:
    """Check whether a character is safe from control/zero-width/bidi smuggling."""
    # Logically identical to `_is_safe_char` in
    # agents/platform/scripts/platform_mcp_server.py, which is the canonical
    # copy: both classify untrusted external text bound for the same model, and
    # a class stripped in one place but not the other is a hole in whichever
    # side forgot. Importing it is not an option — that module builds an MCP
    # server at import time and pulls in `mcp`, `agent_common_server` and
    # `gke_endpoint`, none of which this script has or needs. The mirror is held
    # honest by test_resolver.py's drift test, which compares the two as parsed
    # syntax — so this comment and the docstring may differ from the canonical
    # copy's, and the logic may not.
    code = ord(ch)
    # Preserve newline (\n, 10) and tab (\t, 9)
    if code in (9, 10):
        return True
    # Strip C0 control characters (< 32), DEL (127), and C1 control characters (128-159)
    if code < 32 or 127 <= code <= 159:
        return False
    # Strip zero-width, bidi, and format control characters
    # U+200B-U+200F (Zero-width space, non-joiner, joiner, LRM, RLM)
    # U+202A-U+202E (Bidi embedding/override controls: LRE, RLE, PDF, LRO, RLO)
    # U+2060-U+206F (Word joiner, invisible operators, bidi isolates)
    # U+FEFF (Zero-width no-break space / BOM)
    # U+00AD (Soft hyphen), U+034F (Combining grapheme joiner), U+061C (Arabic letter mark), U+180E (Mongolian vowel separator)
    if (
        0x200B <= code <= 0x200F
        or 0x202A <= code <= 0x202E
        or 0x2060 <= code <= 0x206F
        or code in (0xFEFF, 0x00AD, 0x034F, 0x061C, 0x180E)
    ):
        return False
    # Strip Unicode tag block and non-printable supplementary blocks (U+E0000 and above)
    if code >= 0xE0000:
        return False
    return True


def sanitize_untrusted_text(text: str, max_length: int = 8192) -> str:
    """Sanitizes untrusted external input to neutralize prompt injection attacks."""
    if not text or not isinstance(text, str):
        return ""

    is_truncated = len(text) > max_length
    if is_truncated:
        text = text[:max_length]

    # 1. Strip ANSI escape sequences (7-bit and 8-bit CSI) and carriage returns
    cleaned = re.sub(r"\r", "", text)
    cleaned = re.sub(
        r"(?:\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\x9B[0-?]*[ -/]*[@-~])",
        "",
        cleaned,
    )

    # 2. Strip C0/C1 control characters, DEL, zero-width/bidi characters, and Unicode tag blocks
    cleaned = "".join(ch for ch in cleaned if _is_safe_char(ch))

    # 3. Neutralize prompt injection delimiter tags, instruction markers, and fake system headers.
    #    `[/\s]*` on both sides of the name, not just the front: `</untrusted_title>`,
    #    `< /untrusted_title>` and `<untrusted_title/>` are the same trick, and the
    #    self-closing spelling used to walk through and reach the model looking like
    #    a boundary marker from inside the boundary.
    #    One quantifier each side of the name, and `[^>]*` rather than a lazy
    #    `\s+[^>]*?` followed by another `[/\s]*`. Two quantifiers that can both
    #    match the same run of spaces make the failure case cubic: `<system`
    #    followed by 3,200 spaces and no `>` took 11.7 seconds, 8x per doubling,
    #    and the 8,192-character cap above is the only bound on it. Any GitHub
    #    account can put that in an issue body, and `poll` sanitizes the title,
    #    the body and every comment on every tick.
    cleaned = re.sub(
        r"<[/\s]*(system|instruction|prompt|context|admin|untrusted_[a-z0-9_-]+)\b[^>]*>",
        r"[\1_tag_neutralized]",
        cleaned,
        flags=re.IGNORECASE,
    )
    #    `(?<!\`)` is what keeps this linear. Without it a match can start at
    #    every backtick in a run, and each start consumes to the end of the run
    #    and backtracks through it — quadratic, 1,039 ms on the 8,192 backticks
    #    the cap allows. `poll` sanitizes every comment on the issue, so ~291 of
    #    them crossed RESOLVER_TIMEOUT_S; the lookbehind lets only the first
    #    backtick of a run start a match and brings the same input to 0.34 ms.
    #    `[^\S\n]*` rather than `\s*` so a fence cannot be matched to a keyword
    #    on a later line.
    cleaned = re.sub(
        r"(?<!`)`{3,}[^\S\n]*(system|instruction|prompt)\b",
        r"```text",
        cleaned,
        flags=re.IGNORECASE,
    )
    #    Kept in step with `_neutralize_tokens` in
    #    agents/platform/scripts/platform_mcp_server.py. The same framing reaching
    #    the same model by two routes must not be defused on one and passed through
    #    on the other: `<TOOL_CALL>`, `<USER_REQUEST>`, `### Instruction:` and a
    #    counterfeit `[SECURITY NOTICE:` were all neutralized on the pod-diagnostics
    #    path and verbatim on this one.
    cleaned = re.sub(
        r"\[/?INST\]|<<SYS>>|<\|im_start\|>|<\|im_end\|>"
        r"|###\s*(?:system|instruction):"
        r"|</?(?:USER_REQUEST|TOOL_CALL)>"
        r"|(?:===\s*)?\[SECURITY\s+NOTICE:",
        "[instruction_marker_neutralized]",
        cleaned,
        flags=re.IGNORECASE,
    )

    if is_truncated:
        cleaned += f"\n\n[TRUNCATED: Exceeded {max_length} character limit]"

    return cleaned.strip()


def _label_names(issue: dict) -> set[str]:
    """Extracts a normalized lowercased set of label names from an issue dictionary."""
    labels_raw = issue.get("labels") or []
    label_names = set()
    for l in labels_raw:
        if isinstance(l, dict):
            name = l.get("name", "")
        elif isinstance(l, str):
            name = l
        else:
            name = ""
        if name:
            label_names.add(name.lower())
    return label_names


def calculate_issue_priority(issue: dict) -> tuple[int, str]:
    """Calculates multi-factor priority score and priority label for an issue.
    Returns (score, priority_label).
    """
    label_names = _label_names(issue)

    score = 0
    priority_label = "UNLABELLED"

    # Priority / Severity weighting
    if any(
        l in label_names
        for l in [
            "priority:critical",
            "priority:p0",
            "severity:critical",
            "blocker",
        ]
    ):
        score += 1000
        priority_label = "P0"
    elif any(
        l in label_names
        for l in ["priority:high", "priority:p1", "severity:high"]
    ):
        score += 500
        priority_label = "P1"
    elif any(
        l in label_names for l in ["priority:medium", "priority:p2", "bug"]
    ):
        score += 100
        priority_label = "P2"
    elif any(
        l in label_names
        for l in [
            "priority:low",
            "priority:p3",
            "enhancement",
            "documentation",
        ]
    ):
        score += 10
        priority_label = "P3"

    return score, priority_label


def _fetch_comments(repo: str, number) -> list:
    """Fetch one issue's comments, after the ranking has picked a winner.

    Split out of the list query so that query can widen to 100 issues without
    paying a GraphQL round trip per issue for a field only the selected issue
    needs.

    Returns [] rather than raising when the fetch fails. The comments are
    context for the investigation, not the thing being investigated: an issue
    the agent can still read the title and body of is worth reporting, and a
    poll that died here would take the whole FOUND payload with it.

    A failure is warned about on stderr, though, because the payload cannot
    tell the two apart: `"comments": []` is what an issue with no comments
    looks like too, so an investigation that silently lost the reporter's
    follow-up context would read as a complete one.
    """
    res = run_gh(
        ["issue", "view", str(number), "-R", repo, "--json", "comments"],
        check=False,
    )
    if res.returncode != 0:
        print(
            f"Warning: could not fetch comments for issue #{number}; "
            "continuing with title and body only.",
            file=sys.stderr,
        )
        return []
    try:
        payload = json.loads(res.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    comments = payload.get("comments") if isinstance(payload, dict) else None
    return comments if isinstance(comments, list) else []


def handle_poll(args):
    try:
        repos = get_managed_github_repos()
    except Exception as e:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason": "CONFIGMAP_READ_FAILED",
                    "error": str(e),
                }
            )
        )
        return

    repos = [r for r in repos if BARE_REPO_RE.match(r)]
    if not repos:
        print(json.dumps({"status": "NOT_CONFIGURED"}))
        return

    # Check auth pre-flight safely. A repo is configured but credentials are
    # broken: that is a real fault, so it must NOT be reported as NO_ISSUES
    # (which the skill silences) or the resolver goes quiet forever.
    #
    # A failed pre-flight is not yet evidence of that fault. The credential it
    # fails on is short-lived by construction -- the GitHub App installation
    # token the broker mints expires after an hour, while this poller runs every
    # ten minutes -- so an expired token is the expected steady state between
    # refreshes. run_gh mints once and retries, so by the time this returns
    # non-zero a *freshly minted* token was also rejected. Before that retry
    # existed, an ordinary expiry was reported as GITHUB_AUTH_NOT_CONFIGURED,
    # which sent operators hunting for configuration that was already correct
    # while the watcher stayed silent about real issues for the rest of the
    # token's life.
    auth = run_gh(["auth", "status"], check=False)

    if auth.returncode != 0:
        # Three faults, three operators, three reason codes -- collapsing them
        # is the conflation that made this failure unreadable to begin with.
        # A broker that refused is not a missing binary and neither is a
        # credential nobody ever configured.
        if is_refresh_failed():
            reason = "GITHUB_TOKEN_REFRESH_FAILED"
        elif auth.returncode == GH_MISSING_RC:
            reason = "GH_CLI_NOT_FOUND"
        else:
            reason = "GITHUB_AUTH_NOT_CONFIGURED"
        print(json.dumps({"status": "ERROR", "reason": reason}))
        return

    all_issues = []
    unreachable_repos = []

    for repo in repos:
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
        #
        # `comments` is deliberately absent from this projection and `--limit` is
        # 100 rather than 10, and the two go together. Ranking by priority only
        # reorders the rows the query returned, and which rows those are is not
        # something this query gets to decide: `--search` goes to the search API,
        # whose ordering without a `sort:` qualifier is GitHub's relevance ranking
        # rather than anything this code can predict. At a limit of 10 the ranking
        # therefore re-sorted an arbitrary handful and a P0 outside it was never a
        # candidate — the delay the ranking was added to remove. Widening the window
        # is what makes the ranking mean anything, and 100 covers the whole
        # unaddressed backlog of a repository this agent is plausibly pointed at.
        # It is affordable only because `comments` is dropped — that field costs one
        # GraphQL round trip per issue, so asking for it across 100 issues is what
        # would blow `github_scan_gate`'s RESOLVER_TIMEOUT_S. The winner's comments
        # are fetched on their own below, once there is exactly one issue to fetch
        # them for; that is one list call plus one view call, against the ten
        # issues' worth of comment round trips the old projection paid every tick.
        res = run_gh(
            [
                "issue",
                "list",
                "-R",
                repo,
                "--search",
                search_query,
                "--json",
                "number,title,body,labels,createdAt",
                "--limit",
                "100",
            ],
            check=False,
        )
        if res.returncode != 0:
            unreachable_repos.append(repo)
            continue

        try:
            issues = json.loads(res.stdout)
            if not isinstance(issues, list):
                unreachable_repos.append(repo)
                continue
        except Exception:
            unreachable_repos.append(repo)
            continue

        for issue in issues:
            issue["_repo"] = repo
            all_issues.append(issue)

    if not all_issues:
        if unreachable_repos and len(unreachable_repos) == len(repos):
            print(
                json.dumps(
                    {
                        "status": "ERROR",
                        "reason": "REPO_UNREACHABLE",
                        "unreachable_repos": unreachable_repos,
                    }
                )
            )
            return
        print(
            json.dumps(
                {
                    "status": "NO_ISSUES",
                    "managed_repos": repos,
                    "unreachable_repos": unreachable_repos,
                }
            )
        )
        return

    # Select issue by highest priority score, then earliest creation date and lowest issue number (FIFO tie-breaker)
    scored_issues = []
    for x in all_issues:
        score, label = calculate_issue_priority(x)
        created_at = x.get("createdAt") or ""
        scored_issues.append((score, created_at, int(x["number"]), label, x))

    scored_issues.sort(key=lambda item: (-item[0], item[1], item[2]))

    _, _, _, priority_label, target = scored_issues[0]
    repo = target["_repo"]

    raw_title = target.get("title") or ""
    sanitized_title = sanitize_untrusted_text(raw_title)
    raw_body = target.get("body") or ""
    sanitized_body = sanitize_untrusted_text(raw_body)
    comments = []
    for c in _fetch_comments(repo, target["number"]):
        author = c.get("author") if isinstance(c.get("author"), dict) else {}
        # A GitHub login is `[A-Za-z0-9-]` and at most 39 characters, so there
        # is nothing here for a boundary tag to defend against; wrapping it only
        # put markup in front of every reader of this field. Sanitized anyway,
        # because the cost is nil and the assumption is GitHub's to break.
        comments.append(
            {
                "author": sanitize_untrusted_text(author.get("login") or "unknown"),
                "createdAt": c.get("createdAt", ""),
                "body": f"<untrusted_comment>{sanitize_untrusted_text(c.get('body') or '')}</untrusted_comment>",
            }
        )

    print(
        json.dumps(
            {
                "status": "FOUND",
                "repository": repo,
                "issue_number": target["number"],
                "priority": priority_label,
                "title": f"<untrusted_title>{sanitized_title}</untrusted_title>",
                "title_plain": sanitized_title,
                "body": f"<untrusted_body>{sanitized_body}</untrusted_body>",
                "comments": comments,
                "unreachable_repos": unreachable_repos,
            },
            indent=2,
        )
    )


def _validate_repo_or_exit(repo: str) -> None:
    if not repo or not is_valid_repo_slug(repo):
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason": "INVALID_REPOSITORY",
                    "error": f"Invalid repository format: {repo!r}",
                }
            )
        )
        sys.exit(1)
    try:
        managed = get_managed_github_repos()
    except Exception as e:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason": "CONFIGMAP_READ_FAILED",
                    "error": str(e),
                }
            )
        )
        sys.exit(1)
    if repo not in managed:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "reason": "UNMANAGED_REPOSITORY",
                    "error": f"Repository {repo!r} is not in the managed repositories list: {managed}",
                }
            )
        )
        sys.exit(1)


def handle_claim(args):
    repo = args.repo
    _validate_repo_or_exit(repo)
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
    repo = args.repo
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

    _validate_repo_or_exit(repo)

    # The report is read here and posted on fd 0. A path would not work: this
    # script runs in the sandbox, `gh` runs in the broker pod, and the two share
    # no filesystem. `--body-file` rather than the `-F` short form it replaces —
    # the shim matches the flag to decide whether to forward stdin, and a
    # one-letter flag is the kind that means something else to another tool.
    with open(real_report_path, "r", encoding="utf-8") as handle:
        report_text = handle.read()
    run_gh(
        ["issue", "comment", issue_num, "-R", repo, "--body-file", BODY_STDIN],
        stdin=report_text,
    )

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
    claim_parser.add_argument(
        "--repo", required=True, help="Target repository to act upon."
    )

    # transition
    trans_parser = subparsers.add_parser(
        "transition", help="Upload report and transition issue label/state."
    )
    trans_parser.add_argument(
        "--issue", required=True, type=int, help="Issue number to transition."
    )
    trans_parser.add_argument(
        "--repo", required=True, help="Target repository to act upon."
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
