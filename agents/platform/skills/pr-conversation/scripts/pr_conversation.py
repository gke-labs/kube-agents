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
import sys
import tempfile

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

# How much of a thread travels with the requests. Both caps are generous enough
# that no ordinary review conversation meets them, and both report what they
# dropped — `omitted_earlier` on the thread, `truncated_chars` on the comment —
# because a silently shortened transcript reads as a complete one, and the
# worker would answer confidently from a conversation it only half saw.
CONTEXT_MAX_COMMENTS = 40
CONTEXT_MAX_BODY_CHARS = 4000


def _fail(message: str):
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _resolve_repo() -> str:
    try:
        repo = forge.target_repo()
    except forge.ForgeError as error:
        _fail(f"{error.reason}: {error.value}")
    if not repo:
        _fail("No target repository configured in SETTINGS.md.")
    return repo


def _find_pr(provider, repo: str, number: int):
    for pr in provider.list_open_prs(repo):
        if pr.number == number:
            return pr
    _fail(f"{repo}#{number} is not an open pull request.")


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


def _conversation(comments, self_login: str, request_ids) -> tuple[list, int]:
    """The thread one pull request's requests arrived in, oldest first.

    Sorted here as well as in the provider: ordering is part of this payload's
    contract — "who said what, in what order" is most of the context — and a
    provider that returns its endpoints unmerged should not silently degrade it.

    When the cap bites it drops the *oldest* comments, the opposite of the
    sweep's oldest-first rule for triggers. A trigger is a queue and starving
    its head is unfair; a transcript is a story and its recent end is the part
    that explains the request being answered now.
    """
    ordered = sorted(comments, key=lambda c: (c.created_at, c.node_id))
    omitted_earlier = max(0, len(ordered) - CONTEXT_MAX_COMMENTS)
    rows = []
    for comment in ordered[omitted_earlier:]:
        body, truncated = _context_body(comment.body)
        row = {
            "comment_id": comment.node_id,
            "author": comment.author,
            "created_at": comment.created_at,
            "kind": comment.kind,
            "can_write": comment.can_write,
            "is_self": forge.normalise_login(comment.author) == self_login,
            "is_request": comment.node_id in request_ids,
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
        repo = forge.target_repo()
    except forge.ForgeError as error:
        print(json.dumps({"status": "ERROR", "reason": error.reason, "value": error.value}))
        return 0
    if not repo:
        print(json.dumps({"status": "NOT_CONFIGURED"}))
        return 0

    provider = forge.provider_for()
    try:
        provider.preflight()
        prs = [
            pr
            for pr in provider.list_open_prs(repo)
            if pr.is_agent_authored and not pr.is_ignored
        ]
        if args.pr:
            prs = [pr for pr in prs if pr.number == args.pr]

        found = []
        threads = []
        for pr in prs:
            self_login = provider.self_login(pr)
            comments = provider.list_comments(repo, pr)
            handled = pr_triggers.handled_node_ids(comments, self_login)
            pr_requests = []
            for comment in comments:
                if comment.node_id in handled:
                    continue
                if forge.normalise_login(comment.author) == self_login:
                    continue
                trigger = pr_triggers.find_trigger(
                    comment.body, self_login, comment.node_id, comment.author
                )
                if trigger is None:
                    continue
                pr_requests.append(
                    {
                        "pr": pr.number,
                        "head_ref": pr.head_ref,
                        "comment_id": comment.node_id,
                        "author": comment.author,
                        "can_write": comment.can_write,
                        "kind": trigger.kind,
                        "request": trigger.request,
                        "created_at": comment.created_at,
                        "path": comment.path,
                        "line": comment.line,
                    }
                )
            if not pr_requests:
                # No thread without a request in it: the worker is answering
                # something, and a transcript of a pull request nobody addressed
                # is prompt it has no use for.
                continue
            found.extend(pr_requests)
            rows, omitted_earlier = _conversation(
                comments, self_login, {row["comment_id"] for row in pr_requests}
            )
            thread = {"pr": pr.number, "head_ref": pr.head_ref, "comments": rows}
            if omitted_earlier:
                thread["omitted_earlier"] = omitted_earlier
            threads.append(thread)
    except forge.ForgeError as error:
        print(json.dumps({"status": "ERROR", "reason": error.reason, "value": error.value}))
        return 0

    if not found:
        print(json.dumps({"status": "NO_REQUESTS", "repository": repo}))
        return 0
    # Every request, not just the trusted ones: the worker is told about a
    # request it must not act on so it can say so, rather than appearing to
    # have missed it. `can_write` is on each row and the SKILL.md is explicit
    # that a false one is refused.
    print(
        json.dumps(
            {
                "status": "FOUND",
                "repository": repo,
                "requests": found,
                "conversations": threads,
            }
        )
    )
    return 0


def _post(args, marker_kind: str) -> int:
    repo = _resolve_repo()
    provider = forge.provider_for()
    provider.preflight()
    pr = _find_pr(provider, repo, args.pr)

    body = _confined_body(args.body_file).rstrip()
    stamped = f"{body}\n\n{pr_triggers.marker(args.comment_id, marker_kind)}\n"

    # The stamped copy stays inside scratch: same confinement as the input, and
    # the same directory the skill is already allowed to write.
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", dir=SCRATCH_DIR, delete=False
    )
    try:
        handle.write(stamped)
        handle.close()
        provider.post_comment(repo, pr, handle.name)
    except forge.ForgeError as error:
        _fail(f"could not post to {repo}#{args.pr}: {error}")
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass

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
    poll.add_argument("--pr", type=int, default=0, help="limit to one pull request")
    poll.set_defaults(func=handle_poll)

    for name, func, help_text in (
        ("reply", handle_reply, "post an answer and mark the request answered"),
        ("refuse", handle_refuse, "post a refusal and mark the request refused"),
    ):
        cmd = sub.add_parser(name, help=help_text)
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
        cmd.set_defaults(func=func)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
