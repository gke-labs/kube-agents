#!/usr/bin/env python3
"""
resolver.py — Deterministic helper script for the github-issue-resolver skill.

Label management, the sweep that unsticks abandoned investigations, the poll
that picks the next issue worth a model turn, and the report that closes it.

Every forge call goes through the version-control verbs on the credential
broker rather than through a CLI. The credential lives on the broker's side of
the boundary and is never on this one; the vocabulary — `issue-list`,
`issue-update`, `label-ensure` — is the neutral one, so nothing in this file
names, or needs to know, which forge answered.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# The shared scripts dir holds vcs_client (docker-entrypoint.sh keeps executable
# scripts shared across profiles rather than copying them into each one). The
# third entry is the same directory in a source checkout. Mirrors fleet-audit's
# audit_report, which needs the same modules for the same reason.
# Off when this file is the trusted copy -- see the same block in vcs_client.py.
TRUSTED_CLOSURE = "/opt/vcs/libexec/platform"
if not str(Path(__file__).resolve()).startswith(TRUSTED_CLOSURE + "/"):
    sys.path.append("/opt/defaults/scripts")
    sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

# `sandbox_exec` has no import-time dependency on anything under /opt — the yaml
# it needs to read the managed config is imported inside the function that reads
# it, and a missing config just means "no sandbox".
import sandbox_exec  # noqa: E402 — needs the sys.path lines above
import vcs_client  # noqa: E402 — needs the sys.path lines above
from gitops_workspace import (  # noqa: E402 — needs the sys.path lines above
    GITOPS_STATE_READ_TIMEOUT_SECONDS,
    get_managed_github_repos,
    is_valid_repo_slug,
)

SCRATCH_DIR = "/opt/data/scratch"

#: The reason reported for a broker failure that carries no code of its own:
#: the broker unreachable, its token volume unprojected, `CREDENTIAL_PROXY_URL`
#: unset, an answer that is not JSON. Every one of those is a fault on this side
#: of the seam, and none of them is `FORGE_CALL_FAILED` -- that is the broker's
#: own word for a forge that did not answer *it*, and reporting it here sent an
#: operator to the forge for a broker that was restarting.
BROKER_UNREACHABLE = "BROKER_UNREACHABLE"

# Which copy of this file the forwarded `poll` runs, and it is deliberately not
# the one the model has. The image also bakes the skills tree into
# /opt/defaults/skills and the entrypoint syncs it onto the volume under
# $HERMES_HOME (/opt/data in the sandbox), but that tree is `chown agent:agent`:
# uid 1000 can rewrite it and the edit stands until the next restart. `_forward`
# crosses as `hermes`, which holds the cron's credential and a 0700 home the
# model must not be able to author into, so it must not execute anything the
# model can write -- the rule deploy/sandbox/Dockerfile states above its
# /opt/defaults chown. So the forwarded path is the root-owned staging the
# Dockerfile builds, whose whole import closure is staged with it and checked at
# build time by deploy/sandbox/trusted-closure-guard.py. Same constant, same
# reason and the same directory as `forge.SANDBOX_FORGE`.
SANDBOX_RESOLVER = "/opt/vcs/libexec/platform/resolver.py"

# Bounds the ssh hop around a forwarded subcommand, and it is deliberately the
# caller's own budget rather than a number of this file's choosing.
# `github_scan_gate` allows a poll `RESOLVER_TIMEOUT_S` per managed repository,
# because the work scales with them; a fixed ceiling here would either sit above
# that and never fire, or below it and kill a legitimate poll of a fleet with
# several repositories in it. The margin is what makes this one fire first, so a
# hung hop is reported as `SANDBOX_UNREACHABLE` rather than killed from outside
# -- an outer kill reaches this process and orphans the ssh child. What that
# margin has to be large enough for is below.
#
# It is a copy of the number rather than an import: this module is forwarded
# into the sandbox, where every import it makes has to be a root-owned file in
# the trusted closure, and pulling the gate in to read one integer would put
# the whole scanner there. `test_resolver` pins the two equal instead, so the
# copy cannot drift.
FORWARD_TIMEOUT_PER_REPO_S = 300

# What the margin has to cover, and it is not a round number: the gate starts
# its clock at `subprocess.run`, and everything this process does before
# `sandbox_exec.run` is entered is spent inside that window. Nearly all of it is
# one thing -- this process's own `get_managed_github_repos()`, the same
# ConfigMap read the gate already paid, bounded by the timeout below. A fixed 15
# did not cover it: on a slow API server -- which is exactly when that read is
# slow -- the gate's kill landed first, and an outer kill reaches this process
# and orphans the ssh child, leaving the far-side `poll` and its stale-issue
# writes running unattended while the tick reports no output rather than
# `SANDBOX_UNREACHABLE`. That is the inversion this margin exists to prevent, so
# it is sized off the read rather than guessed above it. The remaining 15 is
# interpreter startup and the argv handling either side of the read.
#
# The read's own failure is deliberately left undersizing the hop: `repos` falls
# back to 1, so on a fleet of three the hop gets 255s against the gate's 900.
# That errs in the safe direction -- this process fires first, reports
# `SANDBOX_UNREACHABLE` and reaps its child -- and the count it would need to do
# better is one the gate does not pass down.
FORWARD_TIMEOUT_MARGIN_S = GITOPS_STATE_READ_TIMEOUT_SECONDS + 15

IN_PROGRESS = "status:in-progress"
ESCALATION_NEEDED = "status:escalation-needed"

# The labels this skill maintains, and what each one means to a human reading
# the repository rather than to the code.
STATUS_LABELS = (
    (
        IN_PROGRESS,
        "FBCA04",
        "Currently being actively investigated by the Platform Agent",
    ),
    (
        "status:resolved",
        "0E8A16",
        "Issue resolved autonomously by Platform Agent",
    ),
    (
        ESCALATION_NEEDED,
        "B60205",
        "Issue requires human review/SRE action",
    ),
    (
        "agent:ignore",
        "E99695",
        "Permanently ignored by automated issue resolvers",
    ),
)

# An investigation that has not touched its issue in this long has crashed or
# been forgotten, and the label it left behind is hiding the issue from the
# poll that would otherwise pick it up again.
STALE_AFTER_SECONDS = 7200

# The labels that mean "not this poll's to take". The first four are this
# skill's own bookkeeping. `agent:audit` marks a fleet-audit ledger, which that
# skill owns and rewrites in place on every run; `agent:delivery-watch` is the
# same shape one job over — the ledger `chat_delivery_watch.py` keeps of
# scheduled reports that stopped reaching chat, edited and closed by that job
# alone. Asked of the forge as `issue list --without-labels`, not applied to a
# page here: a repository with a hundred claimed issues would otherwise answer
# with a page of exclusions and nothing left, which reads as a quiet
# repository.
SKIP_LABELS = [
    IN_PROGRESS,
    ESCALATION_NEEDED,
    "agent:ignore",
    "status:resolved",
    "agent:audit",
    "agent:delivery-watch",
]

# How wide the poll looks before ranking. `comments` is deliberately not asked
# for at this width, and the two go together. Ranking by priority only reorders
# the rows the query returned, so at a limit of ten the ranking re-sorted an
# arbitrary handful and a P0 outside it was never a candidate — the delay the
# ranking was added to remove. A hundred covers the whole unaddressed backlog
# of a repository this agent is plausibly pointed at, and it is affordable
# because the comments are fetched once, for the winner, by `_fetch_comments`
# below: one list call plus one view call, against the ten issues' worth of
# comment round trips the old projection paid every tick.
#
# It is a window and not the backlog, and the edge is at the old end. The
# exclusions make this a search query, which is ordered `created desc`, so the
# hundred the poll ranks are the hundred newest unaddressed issues: on a
# repository holding more than that, the oldest fall outside the window and no
# tick considers them until enough newer ones are closed or labelled. A larger
# number does not remove that edge, only moves it -- what removes it is
# ordering the query by what the ranking is for, which the search grammar does
# not express.
POLL_WINDOW = 100


def refuse(
    reason: str, error: str, code: str | None = None, **fields: object
) -> None:
    """Report a fault as the JSON envelope every caller of this script reads.

    A traceback is not an answer. `github_scan_gate.py` renders `reason`
    through verbatim into the card it files, and the skill's own rules branch
    on it, so a fault that arrives as a stack trace on stderr is a fault
    nobody downstream can say anything about.
    """
    payload = {"status": "ERROR", "reason": reason, "error": error}
    if code:
        payload["code"] = code
    payload.update(fields)
    print(json.dumps(payload))
    sys.exit(1)


def _forward_timeout(argv) -> int:
    """Seconds to allow the forwarded subcommand, sized like the caller's budget.

    Only `poll` scales with the fleet -- it is the one subcommand that visits
    every managed repository, and it is the one `github_scan_gate` budgets per
    repository. `claim` and `transition` name a single issue, so they get the
    one-repository ceiling and do not pay a ConfigMap read to find that out.
    That read is not free and it is on the model's path for those two.
    """
    repos = 1
    if argv and argv[0] == "poll":
        try:
            repos = max(1, len(get_managed_github_repos()))
        except Exception:
            repos = 1
    return repos * FORWARD_TIMEOUT_PER_REPO_S - FORWARD_TIMEOUT_MARGIN_S

#: Exit codes a process that never ran comes back with: the shell's 127
#: (not found) and 126 (found, not executable), and python's 2 for a script
#: path it could not open. Paired with `_NEVER_RAN` below, because 2 is also
#: argparse's.
_NEVER_RAN_CODES = frozenset({2, 126, 127})
_NEVER_RAN = re.compile(
    r"can't open file|No such file or directory|not found|cannot execute|Permission denied"
)


def _forward_to_sandbox(argv: list) -> int:
    """Re-run this whole subcommand inside the shell sandbox.

    `vcs_client` reaches the broker over `CREDENTIAL_PROXY_URL`, and the agent
    pod does not have one. That is deliberate and permanent: the split that
    moved the model's shell into the sandbox gave the gateway the chat relays
    and the sandbox the credential proxy, so that the pod holding the model's
    API keys holds no forge credential. `poll` runs in the agent pod, as a
    subprocess of `github_scan_gate.py`, so it has to cross.

    It crosses once, as the whole subcommand, rather than once per forge call.
    The old arrangement paid an ssh hop per `gh` invocation — a stale sweep, a
    list, and a comment read per managed repository — against a budget
    (`RESOLVER_TIMEOUT_S`) that is spent on the connection as readily as on the
    forge. Crossing once also keeps the ranking, the sanitizer and the JSON
    envelope on one side of the boundary, so a connection that drops mid-poll
    leaves no half-answer to reassemble.

    Nothing is handed across but the argument vector. `poll` reads nothing from
    the agent pod's filesystem and writes nothing to it; the managed repository
    list comes from the ConfigMap, which the sandbox reads through its own
    kubectl exactly as `claim` and `transition` — which have always run there —
    already do.

    The forwarded process cannot forward again. `sandbox_enabled()` reads the
    agent pod's managed Hermes config, and the sandbox image does not carry it.

    The hop is bounded, which it was not when every `gh` call crossed
    separately. See `_forward_timeout` for why the ceiling is the caller's
    arithmetic and not a constant. A hop that hits it is `SANDBOX_UNREACHABLE`,
    the same answer a refused connection gives, because the two mean the same
    thing to the poll: it has not learned that the repositories are quiet. So
    is a hop that lands on an image with no script at the far end -- see the
    exit codes below.
    """
    timeout = _forward_timeout(list(argv))
    try:
        completed = sandbox_exec.run(
            ["python3", SANDBOX_RESOLVER] + list(argv), timeout=timeout
        )
    except subprocess.TimeoutExpired as expired:
        raise sandbox_exec.SandboxUnavailable(
            f"the sandbox did not answer within {timeout}s"
        ) from expired
    if completed.stdout:
        sys.stdout.write(completed.stdout)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    # A far side that never started is not a verdict about the repositories,
    # and passing its exit code up says it was: the caller reads a non-zero
    # resolver as "the poll ran and refused". The image has shipped without a
    # script this expects before, and the whole tick then looked like an
    # ordinary failure instead of an unreachable sandbox. The shell's own
    # not-found/not-executable codes and python's "can't open file" are the
    # only shapes that mean it -- the resolver's own refusals exit 1, and
    # argparse's 2 carries a different sentence.
    if completed.returncode in _NEVER_RAN_CODES and _NEVER_RAN.search(completed.stderr or ""):
        raise sandbox_exec.SandboxUnavailable(
            f"{SANDBOX_RESOLVER} did not run in the sandbox (exit "
            f"{completed.returncode}): {(completed.stderr or '').strip().splitlines()[-1]}"
        )
    return completed.returncode


def forge(verb: str, payload: dict, repo: str) -> dict:
    """One broker call against one repository.

    Every forge call in this file goes through here, so there is one place that
    knows the repository is named in the payload and one exception type —
    `vcs_client.VcsError` — for a caller to think about. The broker's refusal
    carries a `code` (`FORGE_UNAUTHENTICATED`, `FORGE_NOT_FOUND`,
    `FORGE_RATE_LIMITED`), which is what this script reports upward instead of
    the three hand-rolled reason codes the CLI era needed to tell an expired
    token from a missing binary.
    """
    return vcs_client.forge(verb, dict(payload), repository=repo)


def ensure_labels_exist(repo: str):
    """Make sure this skill's status and governance labels exist.

    Best effort. A label that cannot be created is not a reason to abandon the
    claim it was about to decorate, and `label-ensure` is idempotent — it
    creates the label or updates it in place — so the next run simply asks
    again. The refusal goes to stderr rather than nowhere: a repository whose
    credential cannot write labels will not accept the claim either, and the
    two failures read the same from the outside without it.
    """
    for name, color, description in STATUS_LABELS:
        try:
            forge(
                "label-ensure",
                {"name": name, "color": color, "description": description},
                repo,
            )
        except vcs_client.VcsError as refusal:
            print(
                f"Warning: could not ensure label {name!r} on {repo}: {refusal}",
                file=sys.stderr,
            )


def sweep_stale_issues(repo: str):
    """Escalate investigations that claimed an issue and then went quiet.

    The claim label is what hides an issue from the poll, so an investigation
    that crashed between claiming and reporting parks its issue indefinitely.
    Past the SLA the label comes off, a human is told why on the issue itself,
    and the next tick can see the issue again.

    Nothing here raises. The sweep runs before the query on every managed
    repository, and a repository this credential cannot read is a fact the
    query is about to establish anyway — with a refusal code attached. Failing
    here would lose the other repositories' polls to it.
    """
    try:
        # The whole window, not a default page: this exists to unstick
        # everything that is stuck, and a stale claim left behind by page two
        # is one the poll stays blind to for as long as page one stays full.
        found = forge(
            "issue-list", {"labels": [IN_PROGRESS], "limit": POLL_WINDOW}, repo
        )
    except vcs_client.VcsError as refusal:
        # Not fatal, as the docstring says -- but not silent either. The poll
        # that follows reports the same refusal by code; this is where the
        # message itself survives, for whoever reads the worker's stderr.
        print(
            f"resolver: stale-issue sweep skipped for {repo}: "
            f"{refusal.code or BROKER_UNREACHABLE}: {refusal}",
            file=sys.stderr,
        )
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    stale_msg = (
        "🚨 **Autonomous Investigation Timed Out — Human Escalation Required**\n\n"
        "The Platform Agent previously claimed this issue (`status:in-progress`) but no updates were "
        "recorded within the 2-hour SLA window (stale investigation/crash). Transitioning to human review."
    )

    for issue in found.get("issues") or []:
        updated_str = issue.get("updated")
        if not updated_str:
            continue
        try:
            updated = datetime.datetime.fromisoformat(
                updated_str.replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if (now - updated).total_seconds() <= STALE_AFTER_SECONDS:
            continue
        number = issue.get("number")
        try:
            # The comment first, then the labels. A reader who finds the
            # escalation label with no explanation beside it has to guess
            # whether a human moved it; the reverse order costs nothing and is
            # recoverable, since the next sweep sees the claim label still on.
            forge("issue-comment", {"number": number, "body": stale_msg}, repo)
            forge(
                "issue-update",
                {
                    "number": number,
                    "labelsAdd": [ESCALATION_NEEDED],
                    "labelsRemove": [IN_PROGRESS],
                },
                repo,
            )
        except vcs_client.VcsError as refusal:
            print(
                f"Warning: could not escalate stale issue #{number} on {repo}: "
                f"{refusal}",
                file=sys.stderr,
            )


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


def _fetch_comments(repo: str, number) -> tuple[list, bool]:
    """One issue's comments and whether that is all of them, after the ranking.

    Split out of the list query so that query can widen to a hundred issues
    without paying for a field only the selected issue needs.

    `limit` is passed explicitly. Omitting it is not "no ceiling" -- the verb
    validates an absent limit into its own default of 30, which is below what
    the `gh issue view --json comments` call this replaces returned, and the
    comments come oldest first, so the thirty-first is the reporter's newest
    follow-up and exactly the one an investigation wants.

    Returns [] rather than raising when the fetch fails. The comments are
    context for the investigation, not the thing being investigated: an issue
    the agent can still read the title and body of is worth reporting, and a
    poll that died here would take the whole FOUND payload with it.

    A failure is warned about on stderr, though, because the payload cannot
    tell the two apart: `"comments": []` is what an issue with no comments
    looks like too, so an investigation that silently lost the reporter's
    follow-up context would read as a complete one. A page that filled up is
    the same problem one step milder, and the verb reports it
    (`commentsTruncated`) rather than leaving it to be guessed at, so it is
    warned about on stderr and carried on the payload.
    """
    try:
        answer = forge(
            "issue-view",
            {"number": int(number), "comments": True, "limit": POLL_WINDOW},
            repo,
        )
    except (vcs_client.VcsError, ValueError, TypeError) as refusal:
        print(
            f"Warning: could not fetch comments for issue #{number} ({refusal}); "
            "continuing with title and body only.",
            file=sys.stderr,
        )
        return [], False
    truncated = bool(answer.get("commentsTruncated"))
    if truncated:
        print(
            f"Warning: issue #{number} has more than {POLL_WINDOW} comments; "
            "the newest are not in this investigation's context.",
            file=sys.stderr,
        )
    comments = answer.get("comments")
    return (comments if isinstance(comments, list) else []), truncated


def handle_poll(args):
    try:
        repos = get_managed_github_repos()
    except Exception as e:
        refuse("CONFIGMAP_READ_FAILED", str(e))

    repos = [r for r in repos if is_valid_repo_slug(r)]
    if not repos:
        print(json.dumps({"status": "NOT_CONFIGURED"}))
        return

    all_issues = []
    # Repository -> the broker's refusal code. Keyed by code rather than
    # counted, because the codes are what tell an install whose credential the
    # forge has stopped accepting from an install pointed at a repository that
    # does not exist, and those two faults belong to different people.
    refusals: dict = {}
    # Repository -> the refusal's own words. The code is what the rules branch
    # on; the message is what names the broker and the errno when there is no
    # code, and throwing it away left "every managed repository refused the
    # listing" as the whole of what an operator got for a broker restart.
    errors: dict = {}

    for repo in repos:
        # Sweep stale issues first, so an investigation that crashed since the
        # last tick releases its issue in time for this one to pick it up.
        sweep_stale_issues(repo)

        try:
            found = forge(
                "issue-list",
                {
                    "state": "open",
                    "excludeLabels": SKIP_LABELS,
                    "limit": POLL_WINDOW,
                },
                repo,
            )
        except vcs_client.VcsError as refusal:
            # Not fatal on its own. `auth status` used to stand in front of
            # this loop because a CLI could not say whether a failed list was a
            # dead credential, a repository that does not exist, or a scope the
            # token was never granted -- all three exited the same way, so the
            # pre-flight guessed at the difference from a second call. The
            # broker answers it directly, in the refusal, which is why there is
            # no pre-flight here any more.
            #
            # A refusal with no code is not the forge's: it is the transport's
            # -- the broker unreachable, its token missing, an answer that was
            # not JSON -- and it gets a code of this side's rather than the
            # broker's word for a forge fault.
            refusals[repo] = refusal.code or BROKER_UNREACHABLE
            errors[repo] = str(refusal)
            print(f"resolver: {repo}: {refusals[repo]}: {refusal}", file=sys.stderr)
            continue

        for issue in found.get("issues") or []:
            issue["_repo"] = repo
            all_issues.append(issue)

    unreachable_repos = [repo for repo in repos if repo in refusals]

    if not all_issues:
        if len(refusals) == len(repos):
            # Every managed repository refused, so this is not a quiet fleet.
            # When they all refused for the same reason that reason is the
            # answer -- a credential the forge has stopped accepting is one
            # fact about the install, not N facts about N repositories -- and
            # when they disagree, the codes are reported per repository and the
            # status stays the generic one.
            codes = set(refusals.values())
            reason = codes.pop() if len(codes) == 1 else "REPO_UNREACHABLE"
            # The message travels with the code. One message when every
            # repository said the same thing -- a broker down is one sentence,
            # not N -- and the generic line with the per-repository words
            # beside it otherwise.
            messages = set(errors.values())
            refuse(
                reason,
                messages.pop() if len(messages) == 1 else (
                    "every managed repository refused the listing"
                ),
                unreachable_repos=unreachable_repos,
                refusals=refusals,
                errors=errors,
            )
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
        created_at = x.get("created") or ""
        scored_issues.append((score, created_at, int(x["number"]), label, x))

    scored_issues.sort(key=lambda item: (-item[0], item[1], item[2]))

    _, _, _, priority_label, target = scored_issues[0]
    repo = target["_repo"]

    raw_title = target.get("title") or ""
    sanitized_title = sanitize_untrusted_text(raw_title)
    raw_body = target.get("body") or ""
    sanitized_body = sanitize_untrusted_text(raw_body)
    comments = []
    fetched, comments_truncated = _fetch_comments(repo, target["number"])
    for c in fetched:
        # A forge login is short and alphanumeric, so there is nothing here for
        # a boundary tag to defend against; wrapping it only put markup in
        # front of every reader of this field. Sanitized anyway, because the
        # cost is nil and the assumption is the forge's to break.
        comments.append(
            {
                "author": sanitize_untrusted_text(c.get("author") or "unknown"),
                "createdAt": c.get("created", ""),
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
                # The model reads this. An investigation working from a
                # conversation it cannot see all of should say so in its report
                # rather than conclude from a partial one.
                "comments_truncated": comments_truncated,
                "unreachable_repos": unreachable_repos,
            },
            indent=2,
        )
    )


def _validate_repo_or_exit(repo: str) -> None:
    if not repo or not is_valid_repo_slug(repo):
        refuse("INVALID_REPOSITORY", f"Invalid repository format: {repo!r}")
    try:
        managed = get_managed_github_repos()
    except Exception as e:
        refuse("CONFIGMAP_READ_FAILED", str(e))
    if repo not in managed:
        refuse(
            "UNMANAGED_REPOSITORY",
            f"Repository {repo!r} is not in the managed repositories list: {managed}",
        )


def handle_claim(args):
    repo = args.repo
    _validate_repo_or_exit(repo)
    issue_num = int(args.issue)
    ensure_labels_exist(repo)

    forge("issue-update", {"number": issue_num, "labelsAdd": [IN_PROGRESS]}, repo)
    claim_msg = (
        "🤖 **Platform Agent Triaging:** Issue marked `status:in-progress`. "
        "Beginning root cause investigation and recording worklog..."
    )
    forge("issue-comment", {"number": issue_num, "body": claim_msg}, repo)

    print(
        json.dumps(
            {
                "status": "CLAIMED",
                "issue_number": issue_num,
                "repository": repo,
            },
            indent=2,
        )
    )


def handle_transition(args):
    repo = args.repo
    issue_num = int(args.issue)
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

    # The report is read here and sent as the comment's body. A path would not
    # work: this file is on the sandbox's filesystem and the forge call is made
    # from the broker, which shares none of it.
    with open(real_report_path, "r", encoding="utf-8") as handle:
        report_text = handle.read()
    forge("issue-comment", {"number": issue_num, "body": report_text}, repo)

    # Then the labels, then the close. The report goes first and the file is
    # removed last, so a refusal anywhere in between leaves the report on disk
    # for the retry rather than losing the investigation that produced it.
    forge(
        "issue-update",
        {
            "number": issue_num,
            "labelsAdd": [f"status:{state}"],
            "labelsRemove": [IN_PROGRESS],
        },
        repo,
    )

    if state == "resolved":
        forge("issue-close", {"number": issue_num, "reason": "completed"}, repo)

    # Cleanup temporary report file
    try:
        os.remove(real_report_path)
    except Exception:
        pass

    print(
        json.dumps(
            {
                "status": "TRANSITIONED",
                "issue_number": issue_num,
                "new_state": state,
                "repository": repo,
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(
        description="Deterministic issue resolver helper."
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

    # Parsed first, so a malformed command is answered here rather than over
    # ssh by a copy of this file whose usage text the caller cannot see.
    if sandbox_exec.sandbox_enabled():
        try:
            sys.exit(_forward_to_sandbox(sys.argv[1:]))
        except sandbox_exec.SandboxUnavailable as unreachable:
            # A poll that could not reach the sandbox has not learned that the
            # repositories are quiet, and reporting it as quiet is the outcome
            # this path exists to avoid.
            refuse("SANDBOX_UNREACHABLE", str(unreachable))

    handlers = {
        "poll": handle_poll,
        "claim": handle_claim,
        "transition": handle_transition,
    }
    try:
        handlers[args.subcommand](args)
    except vcs_client.VcsError as refusal:
        # The broker's own code, unflattened. `FORGE_UNAUTHENTICATED` and
        # `FORGE_NOT_FOUND` send an operator to different places, and the
        # skill's rules are written against these names. A refusal with no
        # code is the transport's, not the forge's, and gets this side's name
        # for it -- the same one `poll` uses, so `claim` and `transition` do
        # not call a restarting broker a forge outage.
        refuse(refusal.code or BROKER_UNREACHABLE, str(refusal), code=refusal.code)


if __name__ == "__main__":
    main()
