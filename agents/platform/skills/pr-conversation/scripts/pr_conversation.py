#!/usr/bin/env python3
"""pr_conversation.py — deterministic helper for the pr-conversation skill.

Three subcommands, one job each:

* ``poll`` — print the unanswered requests on one pull request, or all of them,
  as JSON, each alongside the conversation it arrived in. The
  `github-repo-watcher` cron job runs the same logic to decide whether to file a
  card; this exposes it so the worker can re-read the truth in Step 1, and so a
  human debugging a missed trigger can run the exact thing the watcher ran.
* ``reply`` — post a comment from a file and stamp it with the marker that
  records the request as answered.
* ``refuse`` — the same, with the refusal marker, for a request the agent has
  decided it will not act on.

Why ``reply`` writes the marker rather than the model
-----------------------------------------------------
The marker is the whole idempotency scheme: a request is unanswered when no
self-authored comment carries ``<!-- agent-answered:<node-id> -->``. If the model
had to remember to type it, the failure mode of forgetting is not a missing
comment — it is the same request being answered again on every tick, ten minutes
apart, forever. So the marker is appended here, from the ``--comment-id`` the
command already requires, and cannot be forgotten.

Being unforgettable is not enough on its own, because the id is still the
model's to supply. A numeric comment id, a truncated node id, or the id of a
neighbouring comment all produce a marker that matches nothing, which is the
same runaway by a slower road. ``--comment-id`` is therefore checked against the
requests the forge reports as unanswered at that moment, and a mismatch fails
before anything is posted.

Why ``poll`` carries the whole thread and not just the triggers
---------------------------------------------------------------
Being addressed is what *wakes* the agent; it is not the whole of what it needs
to read. "Why this value?" is only answerable against the discussion that
preceded it, and two reviewers may have settled the question between themselves
before anyone typed ``/agent``. So every comment on the pull request travels
with the requests, whether or not it addressed the agent and whether or not its
author has write access — ``is_request``, ``is_self`` and ``can_write`` mark
each one so the worker can weigh it.

That widens what reaches the model: a comment from an account with no write
access is now in the prompt even though it can never be acted on. It is
context, never instruction, and the SKILL.md says so in the terms the model
reads. The trust decision stays exactly where it was — on ``can_write`` of the
comment that did the addressing.

Reply bodies are confined to ``/opt/data/scratch`` by the same ``realpath``
check ``resolver.handle_transition`` uses. The body is posted publicly, so the
path it comes from is bounded rather than merely checked for existence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

# `$HERMES_HOME/scripts`, where the entrypoint's step 2b force-sync stages the
# shared modules. Resolved from the environment rather than by walking up from
# __file__, because the skill directory is a symlink into the profile home and
# the relative path from there is not the path on disk.
_SCRIPTS = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import forge  # noqa: E402
import pr_triggers  # noqa: E402

SCRATCH_DIR = "/opt/data/scratch"

BARE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# How much of a thread travels with the requests. All caps are generous enough
# that no ordinary review conversation meets them, and all report what they
# dropped — `omitted_earlier` and `omitted_requests` on the thread, `truncated_chars`
# on the comment / request — because a silently shortened transcript reads as a
# complete one, and the worker would answer confidently from a conversation it only
# half saw.
CONTEXT_MAX_COMMENTS = 40
CONTEXT_MAX_BODY_CHARS = 4000
CONTEXT_MAX_REQUEST_CHARS = pr_triggers.MAX_REQUEST_CHARS
CONTEXT_MAX_REQUESTS = 10


def _fail(message: str):
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def validate_repo(repo: str) -> str:
    """Ensure repo is formatted as owner/name and is in the managed repos allowlist if configured."""
    from gitops_workspace import get_managed_github_repos, is_valid_repo_slug
    if not repo or not is_valid_repo_slug(repo):
        raise ValueError(f"Invalid repository format: {repo!r}. Expected 'owner/name'.")
    managed = get_managed_github_repos()
    if managed and repo not in managed:
        raise ValueError(
            f"Repository {repo!r} is not in the managed repositories list: {managed}"
        )
    return repo


def _resolve_repo(args=None) -> str:
    if args and getattr(args, "repo", None):
        try:
            return validate_repo(args.repo)
        except ValueError as error:
            _fail(str(error))
    _fail("No target repository specified; pass --repo <owner/repo>.")


def _find_pr(provider, repo: str, number: int, viewer: str):
    """The agent's own open pull request `number`, or exit.

    Scoped by `is_agent_pull_request` rather than by number alone: `reply`
    posts publicly under the agent's identity, and the sweep only ever files
    cards for pull requests the agent opened. A number that resolves to
    somebody else's is a bad card or a bad hand-run, not something to answer.

    `agent:ignore` is honoured for the same reason the sweep honours it, and
    honouring it in only one of the two places would make the label a request
    rather than an opt-out: a card filed before the label went on still runs
    afterwards, and a hand-run never consulted it at all. The label is how a
    maintainer says "stop posting here", and the posting is what it has to
    stop.
    """
    for pr in provider.list_open_prs(repo):
        if pr.number != number:
            continue
        if not forge.is_agent_pull_request(pr, repo, viewer):
            _fail(f"{repo}#{number} is not one of this agent's pull requests.")
        if pr.is_ignored:
            _fail(
                f"{repo}#{number} is labelled {forge.IGNORE_LABEL}, so the agent does not "
                "post on it. Nothing was posted."
            )
        return pr
    _fail(f"{repo}#{number} is not an open pull request.")


def _context_request(request: str) -> tuple[str, int]:
    """A request text as the model should see it, and how much was cut."""
    text = request or ""
    if len(text) <= CONTEXT_MAX_REQUEST_CHARS:
        return text, 0
    return text[:CONTEXT_MAX_REQUEST_CHARS], len(text) - CONTEXT_MAX_REQUEST_CHARS


def _requests_on(provider, repo: str, pr, viewer: str) -> tuple[list, list]:
    """Every comment on one pull request, and the unanswered requests among them.

    One implementation for both readers, because the sweep's filters are
    load-bearing and a second copy that drifted would let the worker act on a
    comment the gate deliberately passed over.
    """
    comments = provider.list_comments(repo, pr)
    handled = pr_triggers.handled_node_ids(comments, viewer)
    allowed_bots = pr_triggers.bot_allowlist()
    requests = []
    for comment in comments:
        if comment.node_id in handled:
            continue
        if forge.normalise_login(comment.author) == viewer:
            continue
        if not pr_triggers.is_addressable_bot(comment, allowed_bots):
            continue
        trigger = pr_triggers.find_trigger(
            comment.body, viewer, comment.node_id, comment.author
        )
        if trigger is None:
            continue
        req_text, truncated = _context_request(trigger.request)
        row = {
            "pr": pr.number,
            "head_ref": pr.head_ref,
            "comment_id": comment.node_id,
            "author": comment.author,
            "can_write": comment.can_write,
            "can_write_known": comment.can_write_known,
            "kind": trigger.kind,
            "request": req_text,
            "created_at": comment.created_at,
            "path": comment.path,
            "line": comment.line,
        }
        if truncated:
            row["truncated_chars"] = truncated
        requests.append(row)
    return comments, requests


def _refusals_already_posted(comments, viewer: str) -> int:
    """Refusals the agent has already written on this pull request.

    Read from the thread rather than tracked, for the same reason the answered
    markers are: the sweep and the worker are separate processes on separate
    schedules, and the thread is the only state both of them can see.
    """
    return len(pr_triggers.refused_node_ids(comments, viewer))


def _refusals_exhausted(comments, viewer: str) -> bool:
    """Whether this pull request has spent its whole refusal budget."""
    return _refusals_already_posted(comments, viewer) >= pr_triggers.max_refusals_per_pr()


def _confined_body(path: str) -> str:
    """The reply body, read from a path confined to the scratch directory.

    Symlinks are resolved before the prefix check, so a link planted inside
    scratch cannot reach outside it.
    """
    scratch = os.path.realpath(SCRATCH_DIR)
    real = os.path.realpath(path)
    if not real.startswith(scratch + os.sep):
        _fail(f"Reply body {path} resolves outside {scratch}.")
    if not os.path.isfile(real):
        _fail(f"Reply body {path} does not exist.")
    with open(real, "r", encoding="utf-8") as handle:
        body = handle.read()
    if not body.strip():
        _fail(f"Reply body {path} is empty.")
    return body


# --------------------------------------------------------------------------


def _context_body(body: str) -> tuple[str, int]:
    """A comment body as the model should see it, and how much was cut."""
    text = pr_triggers.strip_markers(body)
    if len(text) <= CONTEXT_MAX_BODY_CHARS:
        return text, 0
    return text[:CONTEXT_MAX_BODY_CHARS], len(text) - CONTEXT_MAX_BODY_CHARS


def _conversation(
    comments, self_login: str, request_ids, all_request_ids=None
) -> tuple[list, int]:
    """The thread one pull request's requests arrived in, oldest first.

    Sorted here as well as in the provider: ordering is part of this payload's
    contract — "who said what, in what order" is most of the context — and a
    provider that returns its endpoints unmerged should not silently degrade it.

    When the cap bites it drops the *oldest* comments, the opposite of the
    sweep's oldest-first rule for triggers. A trigger is a queue and starving
    its head is unfair; a transcript is a story and its recent end is the part
    that explains the request being answered now.

    The requests themselves are exempt from the cap, and that exemption is the
    difference between a thin transcript and an unanswerable one. The two rules
    point opposite ways: the sweep hands the worker the *oldest* unanswered
    trigger, and the cap drops the *oldest* comments — so on a thread past the
    cap the comment being answered is the first thing thrown away. For a
    `@mention` trigger `Trigger.request` is empty by construction, so the card
    carries no copy of it either, and the worker would be asked to answer a
    request whose text appears nowhere in its context. Pinning costs at most
    `CONTEXT_MAX_REQUESTS` extra rows.
    """
    ordered = sorted(comments, key=lambda c: (c.created_at, c.node_id))
    wanted = set(request_ids or ())
    is_req_ids = set(all_request_ids if all_request_ids is not None else wanted)
    recent = ordered[max(0, len(ordered) - CONTEXT_MAX_COMMENTS) :]
    kept_ids = {c.node_id for c in recent} | wanted
    kept = [c for c in ordered if c.node_id in kept_ids]
    omitted_earlier = len(ordered) - len(kept)
    rows = []
    for comment in kept:
        body, truncated = _context_body(comment.body)
        row = {
            "comment_id": comment.node_id,
            "author": comment.author,
            "created_at": comment.created_at,
            "kind": comment.kind,
            "can_write": comment.can_write,
            "is_self": forge.normalise_login(comment.author) == self_login,
            "is_request": comment.node_id in is_req_ids,
            "body": body,
        }
        if comment.path:
            row["path"] = comment.path
            row["line"] = comment.line
        if truncated:
            row["truncated_chars"] = truncated
        rows.append(row)
    return rows, omitted_earlier


def handle_poll(args) -> int:
    """Report unanswered requests, in the vocabulary the sweep uses.

    Deliberately mirrors ``resolver.py poll``'s status vocabulary
    (``NOT_CONFIGURED`` / ``NO_REQUESTS`` / ``FOUND`` / ``ERROR``) so one
    operator-facing glossary covers both halves of the watcher.
    """
    try:
        from gitops_workspace import get_managed_github_repos
        if getattr(args, "repo", None):
            repos = [validate_repo(args.repo)]
        else:
            repos = get_managed_github_repos()
    except ValueError as error:
        print(json.dumps({"status": "ERROR", "reason": "INVALID_REPOSITORY", "value": str(error)}))
        return 0
    except forge.ForgeError as error:
        print(json.dumps({"status": "ERROR", "reason": error.reason, "value": error.value}))
        return 0
    except Exception as error:
        print(json.dumps({"status": "ERROR", "reason": "DISCOVERY_FAILED", "value": str(error)}))
        return 0
    if not repos:
        print(json.dumps({"status": "NOT_CONFIGURED"}))
        return 0

    provider = forge.provider_for(repo=repos[0] if repos else None)
    try:
        provider.preflight()
        viewer = provider.viewer_login()
        if not viewer:
            print(json.dumps({"status": "ERROR", "reason": "VIEWER_UNKNOWN", "value": ""}))
            return 0
        prs: list[tuple[str, forge.PullRequest]] = []
        for r in repos:
            for pr in provider.list_open_prs(r):
                if forge.is_agent_pull_request(pr, r, viewer) and not pr.is_ignored:
                    if not args.pr or pr.number == args.pr:
                        prs.append((r, pr))

        found = []
        threads = []
        over_budget = 0
        deferred_requests = 0
        for repo, pr in prs:
            comments, pr_requests = _requests_on(provider, repo, pr, viewer)
            # Untrusted requests past this pull request's refusal budget are not
            # offered at all. The sweep already stopped refusing them, on
            # purpose, and handing them to the worker is how that bound got
            # spent twice. `_check_trust` is the enforcement; this is only about
            # keeping work out of the prompt that the worker would then be told
            # it may not do.
            if _refusals_exhausted(comments, viewer):
                # Known-untrusted only. A row whose lookup did not answer is not
                # a stranger and stays visible, so the worker can say it was
                # held rather than appear to have missed a maintainer.
                kept = [
                    row
                    for row in pr_requests
                    if row.get("can_write") or not row.get("can_write_known")
                ]
                over_budget += len(pr_requests) - len(kept)
                pr_requests = kept

            # After the budget filter: cap-deferred rows keep is_request,
            # budget-buried rows stay unflagged.
            all_unanswered_ids = {row["comment_id"] for row in pr_requests}

            # Prioritize trusted requests over untrusted ones so an untrusted burst
            # before refusal budget exhaustion cannot starve maintainer requests.
            trusted_requests = [
                r for r in pr_requests if r.get("can_write") or not r.get("can_write_known")
            ]
            untrusted_requests = [
                r for r in pr_requests if not r.get("can_write") and r.get("can_write_known")
            ]
            ordered_requests = trusted_requests + untrusted_requests

            deferred_count = 0
            if len(ordered_requests) > CONTEXT_MAX_REQUESTS:
                deferred_count = len(ordered_requests) - CONTEXT_MAX_REQUESTS
                deferred_requests += deferred_count
                pr_requests = ordered_requests[:CONTEXT_MAX_REQUESTS]
            else:
                pr_requests = ordered_requests

            if not pr_requests:
                # No thread without a request in it: the worker is answering
                # something, and a transcript of a pull request nobody addressed
                # is prompt it has no use for.
                continue
            found.extend(pr_requests)
            rows, omitted_earlier = _conversation(
                comments,
                viewer,
                request_ids={row["comment_id"] for row in pr_requests},
                all_request_ids=all_unanswered_ids,
            )
            thread = {"repo": repo, "pr": pr.number, "head_ref": pr.head_ref, "comments": rows}
            if omitted_earlier:
                thread["omitted_earlier"] = omitted_earlier
            if deferred_count:
                thread["omitted_requests"] = deferred_count
            threads.append(thread)
    except forge.ForgeError as error:
        print(json.dumps({"status": "ERROR", "reason": error.reason, "value": error.value}))
        return 0

    if over_budget:
        # stderr, so it stays out of the JSON the SKILL parses. Recorded rather
        # than silent for the reason the sweep records its own drops: a bound
        # nobody can see reads as "there was nothing there".
        sys.stderr.write(
            f"pr_conversation: {over_budget} untrusted request(s) not offered — "
            "the pull request's refusal budget is spent\n"
        )
    if deferred_requests:
        sys.stderr.write(
            f"pr_conversation: {deferred_requests} request(s) deferred — "
            f"poll offers at most {CONTEXT_MAX_REQUESTS} per pull request\n"
        )

    if not found:
        print(json.dumps({"status": "NO_REQUESTS"}))
        return 0

    payload = {"status": "FOUND", "requests": found, "conversations": threads}
    if over_budget:
        payload["warnings"] = [
            f"pr_conversation: {over_budget} untrusted request(s) not offered — "
            "the per-pull-request refusal budget is exhausted"
        ]
    print(json.dumps(payload))
    return 0


#: Shortest abbreviation accepted for `--verify-commit`. Git's own default.
SHA_MIN_LEN = 7


def _check_trust(
    request: dict,
    marker_kind: str,
    repo: str,
    number: int,
    refused_so_far: int = 0,
) -> None:
    """Enforce here what the SKILL.md only asks for.

    The sweep decides who may be acted for, and it decides it twice over:
    `can_write` for the permission, `can_write_known` for whether the lookup
    answered at all. Nothing re-checked either at post time, so the whole trust
    boundary of this feature was a paragraph of prose in Step 2 of the
    SKILL.md. `poll` hands the worker every comment on the pull request, from
    every author, precisely so it can see the untrusted ones — which means the
    text arguing it should answer anyway is *in the prompt*, next to the row
    saying it must not. A model that misreads one row, or is talked into it by
    the comment it is reading, posts a real answer under the agent's identity
    to an account with no write access to the repository. That is a decision
    this file can check, so it checks it.

    Three rules, and which of them a refusal is subject to is the whole subtlety
    of this function.

    **Write access gates `reply` only.** `refuse` posts no answer and takes no
    action; it declines a request and stamps the marker that stops it being
    handed back every ten minutes. Step 2 of the SKILL.md sends the worker to it
    for exactly the case gated here, and for a trusted reviewer's out-of-scope
    ask too. Blocking a refusal is how a request loops forever, so the
    conservative verdict stays available from either side of that gate.

    **An unanswered permission lookup gates both.** Unknown is not permission,
    and a `reply` on it is the same public post on the same unverified account.
    But a *refusal* on it is worse, and it is the one mistake here that cannot
    be taken back: `agent-refused` closes the request for good, so a proxy
    timeout would silence a maintainer permanently and no later sweep would
    re-open it. The sweep holds these for the next tick rather than refusing
    them, says so in as many words, and this now holds them too. The
    close-the-loop failure the paragraph above guards against does not apply,
    because the request is not being declined — it is being left for a tick when
    the lookup works.

    **The per-pull-request refusal budget gates a refusal of an untrusted
    account.** The sweep stops at `pr_triggers.max_refusals_per_pr()` so that a
    hundred comments from an account with no write access cannot become a
    hundred public comments from the agent. Those requests are dropped without a
    marker, which means they are still unanswered — so `poll` kept handing them
    back, and the next card any trusted reviewer filed had the worker post the
    refusals the sweep had already declined to. Counting here is what makes the
    budget one budget. A refusal of a *trusted* reviewer is not counted against
    it: that is an answer to somebody entitled to one, and the amplification the
    budget exists to stop needs an account that can comment without limit.
    """
    author = request.get("author", "an unknown account")
    where = f"{repo}#{number}"
    refusing = marker_kind == pr_triggers.REFUSED_MARKER
    verb = "declined in the thread" if refusing else "answered"

    if not request.get("can_write_known", False):
        _fail(
            f"whether @{author} has write access to {where} could not be determined, so "
            f"this request may not be {verb}. Nothing was posted, no marker was written, "
            "and the next sweep re-reads it once the lookup works again."
        )

    if refusing:
        budget = pr_triggers.max_refusals_per_pr()
        if not request.get("can_write", False) and refused_so_far >= budget:
            _fail(
                f"{where} has already been refused {refused_so_far} time(s), which is its "
                f"whole budget of {budget}. Requests from accounts without write access are "
                "ignored in silence past it, so nothing was posted and no marker was "
                "written. Say so when you complete the card and leave the request alone."
            )
        return

    if not request.get("can_write", False):
        _fail(
            f"@{author} does not have write access to {where}, so the agent does not act on "
            "their request. Nothing was posted. Use `refuse` to decline it in the thread — "
            "that is what closes the loop without answering it."
        )


def _parse_time(value: str):
    """One forge timestamp as a comparable datetime, or None if unreadable.

    Both timestamps compared here come from the same GitHub API and are
    ISO-8601 with a `Z`, which `fromisoformat` did not accept before 3.11.
    Unreadable returns None and the caller declines to compare rather than
    guessing an ordering.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _check_claim(provider, repo: str, pr, sha: str, no_change: bool, requested_at: str = "") -> None:
    """Refuse to post a claim to have amended the branch until it is true.

    A reply saying "I have bumped the replica count to 2" is checkable, and
    checking it is the difference between a wrong answer and a lie the thread
    keeps. It matters more here than it would elsewhere: the reply is stamped
    `agent-answered`, so the request is closed for good and no later sweep
    re-opens it. Observed live — a worker whose `prepare` step was blocked
    reported both edits as made, stamped the marker, and left a branch still
    holding the old values with nothing to notice it.

    So `reply` makes the model say which world it is in — `--verify-commit
    <sha>` or `--no-change` — and this checks the first against the forge. The
    second is not verifiable here and is not pretended to be; what it buys is
    that a false claim now has to survive the model asserting the opposite one
    line above it, and it leaves that assertion in the command history.

    The two worlds are two flags, not one value. They shared a `dest` once, so
    `--no-change` was `--verify-commit ""` — and therefore `--verify-commit ""`
    was `--no-change`: an empty shell variable, or a `--jq` that returned
    nothing because the branch had no commits yet, satisfied `required=True`,
    skipped every check, and posted the claim. Every other malformed sha fails
    loudly; that one input failed silently, in the unsafe direction, which is
    the one shape this whole check exists to make impossible.

    On the branch is not enough, which is the second bound. Every commit the
    agent ever pushed is on the branch, including the one that opened the pull
    request — so a model that answers "done, see abc1234" while having changed
    nothing passes the membership test by naming its own earlier work. That is
    not a hypothetical: it is the same live failure recorded above, one step
    later, and the check as first written would not have caught it. So the
    commit must also postdate the request it claims to answer. `requested_at`
    is the triggering comment's `created_at`, which the caller already holds
    from the pending-request row it validated `--comment-id` against.
    """
    if no_change:
        return
    if not sha.strip():
        _fail(
            "--verify-commit was given an empty value. Pass the sha of the commit "
            "this reply claims to have made, or --no-change if it made none. An "
            "empty sha is not a claim that nothing changed."
        )
    wanted = sha.strip().lower()
    if len(wanted) < SHA_MIN_LEN:
        _fail(
            f"--verify-commit {sha} is too short to identify a commit; "
            f"give at least {SHA_MIN_LEN} characters."
        )
    try:
        commits = provider.list_commits(repo, pr)
    except forge.ForgeError as error:
        # Unverifiable is not verified. Posting anyway would put the claim in
        # the thread with the marker that closes it.
        _fail(f"could not read the commits on {repo}#{pr.number} to check the claim: {error}")
    shas = [c.sha.lower() for c in commits]
    matched = [c for c in commits if c.sha.lower().startswith(wanted)]
    if not matched:
        _fail(
            f"{sha} is not a commit on {repo}#{pr.number}, so the reply's claim to have "
            "changed the branch is not true yet. Nothing was posted. The branch tip is "
            f"{pr.head_sha or 'unknown'}"
            + (f"; its commits are {', '.join(s[:8] for s in shas)}." if shas else ".")
        )

    asked = _parse_time(requested_at)
    if asked is None:
        # No usable request time — the row carried none, or the forge sent a
        # shape this cannot read. The membership check above still ran; the
        # recency bound is skipped rather than guessed, and says so, because
        # inventing an ordering here would fail in the unsafe direction.
        sys.stderr.write(
            "pr_conversation: request timestamp unreadable, so the commit's recency "
            "was not checked — only that it is on the branch.\n"
        )
        return
    landed = _parse_time(matched[0].committed_at)
    if landed is None:
        _fail(
            f"the forge did not report a date for {sha}, so this reply's claim to have "
            "changed the branch in response to the request cannot be checked. Nothing "
            "was posted."
        )
    if landed < asked:
        _fail(
            f"{sha} was committed at {matched[0].committed_at}, before the request at "
            f"{requested_at}. It is on the branch, but it is not work done in answer to "
            "this request, so the reply's claim is not true. Push the change first, or "
            "pass --no-change if the answer needs none. Nothing was posted."
        )


def _post(args, marker_kind: str) -> int:
    repo = _resolve_repo(args)
    provider = forge.provider_for(repo=repo)

    # Everything that talks to the forge before the post, inside one guard.
    # `handle_poll` turns a `ForgeError` into a reason code the SKILL tells the
    # model to read; leaving these outside the guard meant an auth blip handed
    # it a Python traceback instead, after it had already written the body.
    try:
        provider.preflight()
        viewer = provider.viewer_login()
        if not viewer:
            _fail("the GitHub credential could not name the account it authenticates as.")
        pr = _find_pr(provider, repo, args.pr, viewer)
        comments, requests = _requests_on(provider, repo, pr, viewer)
    except forge.ForgeError as error:
        _fail(f"{error.reason}: {error.value}")

    # The marker closes the request named by `--comment-id`, and the model
    # supplies that id. A numeric id, a truncated node id, or the id of a
    # different comment all post a real answer stamped with a marker that
    # matches nothing — so `handled_node_ids` keeps returning the request, and
    # the sweep re-answers it every ten minutes. That is the exact failure this
    # helper exists to prevent, so the id is checked against the requests the
    # forge reports as unanswered right now rather than trusted.
    pending = {row["comment_id"]: row for row in requests}
    if args.comment_id not in pending:
        _fail(
            f"{args.comment_id} is not an unanswered request on {repo}#{args.pr}. "
            + (
                "Unanswered right now: " + ", ".join(sorted(pending))
                if pending
                else "There are no unanswered requests on it."
            )
        )
    request = pending[args.comment_id]

    # Counted from the thread this call just read, not from what `poll` saw:
    # between the two, the sweep or an earlier command in this same run may have
    # spent the rest of the budget.
    _check_trust(
        request,
        marker_kind,
        repo,
        args.pr,
        _refusals_already_posted(comments, viewer),
    )

    _check_claim(
        provider,
        repo,
        pr,
        getattr(args, "verify_commit", ""),
        # `refuse` never claims a change, so it is not asked and never posts one.
        getattr(args, "no_change", marker_kind == pr_triggers.REFUSED_MARKER),
        request.get("created_at", ""),
    )

    # Marker syntax is stripped out of the model's body before the real marker
    # is appended. `handled_node_ids` reads raw bodies and counts *every* marker
    # in a self-authored comment, so one the model imitated in its own prose
    # becomes a real marker the moment this posts — and a marker naming another
    # node id closes that request for good, at both readers, with silence as the
    # reviewer's only signal. The model holds both halves: SKILL.md prints the
    # syntax in full in order to forbid it, and `poll` carries every node id. A
    # line of prose is not the boundary that belongs in front of that.
    body = pr_triggers.strip_markers(_confined_body(args.body_file))
    if not body:
        _fail(f"Reply body {args.body_file} is nothing but marker syntax.")
    stamped = f"{body}\n\n{pr_triggers.marker(args.comment_id, marker_kind)}\n"

    # The stamped copy is never written down. It used to go to a file in
    # scratch, on the volume the credential sidecar also mounted; with the
    # broker in its own pod there is no such volume, and `post_comment` takes
    # the text. `_confined_body` above still governs where the *input* may come
    # from — that confinement is about what the model is allowed to read, and is
    # unaffected by how the result travels.
    try:
        provider.post_comment(repo, pr, stamped)
    except forge.ForgeError as error:
        _fail(f"could not post to {repo}#{args.pr}: {error}")

    print(
        json.dumps(
            {
                "status": "POSTED",
                "repository": repo,
                "pr": args.pr,
                "comment_id": args.comment_id,
                "marker": marker_kind,
            }
        )
    )
    return 0


def handle_reply(args) -> int:
    return _post(args, pr_triggers.ANSWERED_MARKER)


def handle_refuse(args) -> int:
    return _post(args, pr_triggers.REFUSED_MARKER)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="list unanswered requests as JSON")
    poll.add_argument("--repo", default=None, help="target repository (owner/repo)")
    poll.add_argument("--pr", type=int, default=0, help="limit to one pull request")
    poll.set_defaults(func=handle_poll)

    for name, func, help_text in (
        ("reply", handle_reply, "post an answer and mark the request answered"),
        ("refuse", handle_refuse, "post a refusal and mark the request refused"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument(
            "--repo",
            required=True,
            help="the target repository (owner/repo)",
        )
        cmd.add_argument("--pr", type=int, required=True)
        cmd.add_argument(
            "--comment-id",
            required=True,
            help="the node id of the comment being answered",
        )
        cmd.add_argument(
            "--body-file",
            required=True,
            help=f"path to the comment body, under {SCRATCH_DIR}",
        )
        if name == "reply":
            # Required and exclusive: an answer either changed the branch or it
            # did not, and saying which is what `_check_claim` verifies. A
            # refusal never claims a change, so it is not asked.
            claim = cmd.add_mutually_exclusive_group(required=True)
            claim.add_argument(
                "--verify-commit",
                default=None,
                metavar="SHA",
                help="the commit this reply claims to have made; checked against "
                "the pull request before anything is posted",
            )
            claim.add_argument(
                "--no-change",
                # Its own `dest`, deliberately. Sharing one with
                # `--verify-commit` made the two flags the same value, so an
                # empty sha silently became "nothing changed" — see
                # `_check_claim`.
                dest="no_change",
                action="store_true",
                help="this reply changed nothing on the branch",
            )
        cmd.set_defaults(func=func)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
