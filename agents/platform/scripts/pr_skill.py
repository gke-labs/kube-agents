#!/usr/bin/env python3
"""pr_skill.py — plumbing shared by the skill-side pull-request helpers.

`pr_conversation.py` and `update_pr.py` are separate commands driven by
separate skills, but they do the same four things before they post anything:
resolve the repository they were aimed at and check it is one this install
manages, find the pull request and confirm it is one the agent may write on,
read a body the model wrote from a confined directory, and put that body on
the forge without ever passing it through argv.

Those four are here rather than in either script because three of them are
gates rather than conveniences. `validate_repo` is what stops a write being
aimed at a repository the install was never given, `find_agent_pr` is what
stops a bad card or a mistyped hand-run posting under the agent's identity on
a stranger's pull request, and `confined_body` is what stops a path outside
`/opt/data/scratch` becoming a public comment. A gate with two implementations
is a gate with one implementation and one copy of it that will drift, and the
drift is silent: the copy keeps passing its own tests while permitting what
the original refuses.

This module is deliberately thin on policy. What counts as the agent's own
pull request lives in `forge.is_agent_pull_request`, and what counts as
handled lives in `pr_triggers`; this is the wiring that reads them.
"""

from __future__ import annotations

import os
import sys

import forge

#: The one directory a comment body may be read from. Bodies read from here are
#: posted in public, so the path is bounded rather than merely checked for
#: existence. Nothing is written back into it since #913 — see `post_body`.
SCRATCH_DIR = "/opt/data/scratch"


def fail(message: str):
    """Print an error and exit non-zero.

    Skill helpers are read by a model, which sees stderr and the exit code.
    Every refusal in this module ends here so that no partial write has
    happened by the time the model reads about it.
    """
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def validate_repo(repo: str) -> str:
    """`repo` as `owner/name`, in the primary org, and on the managed
    allowlist, or `ValueError`.

    Three checks rather than one. The slug check is what stops a value that
    reaches a `gh` argument list from carrying path traversal or a leading
    dash; the allowlist is what stops a hand-run or a stale card aiming a
    write at a repository the install was never given; and
    `validate_repo_org` (#1200) is what stops one aimed outside the
    organisation the token minter is bound to, which the allowlist does not
    cover when it is unset.

    The org check is last because it is the only one that can pass vacuously —
    it is a no-op when neither `GITOPS_ORG` nor `GITHUB_ORG` is set — so
    running it after the two that always apply keeps the error a caller sees
    the most specific one available.

    `gitops_workspace` is imported here rather than at module scope because
    this module is force-synced into `$HERMES_HOME/scripts` alongside it, and
    a top-level import would make every consumer of `pr_skill` pay for that
    module's own imports.
    """
    from gitops_workspace import (
        get_managed_github_repos,
        is_valid_repo_slug,
        validate_repo_org,
    )

    if not repo or not is_valid_repo_slug(repo):
        raise ValueError(f"Invalid repository format: {repo!r}. Expected 'owner/name'.")
    managed = get_managed_github_repos()
    if managed and repo not in managed:
        raise ValueError(
            f"Repository {repo!r} is not in the managed repositories list: {managed}"
        )
    return validate_repo_org(repo)


def resolve_repo(args=None) -> str:
    """The repository a write verb was aimed at, or exit.

    Named explicitly rather than discovered. Since the watcher went
    multi-repo there is no single configured target to fall back on, and
    guessing one for a verb that posts or pushes would aim it at whichever
    repository happened to sort first.
    """
    if args and getattr(args, "repo", None):
        try:
            return validate_repo(args.repo)
        except ValueError as error:
            fail(str(error))
    fail("No target repository specified; pass --repo <owner/repo>.")


def find_agent_pr(provider, repo: str, number: int, viewer: str):
    """The agent's own open pull request `number`, or exit.

    Scoped by `is_agent_pull_request` rather than by number alone: these
    helpers post publicly under the agent's identity and push to the branch,
    and the sweeps only ever file cards for pull requests the agent opened. A
    number that resolves to somebody else's is a bad card or a bad hand-run,
    not something to act on.

    `agent:ignore` is honoured for the same reason the sweeps honour it, and
    honouring it in only one of the two places would make the label a request
    rather than an opt-out: a card filed before the label went on still runs
    afterwards, and a hand-run never consulted it at all. The label is how a
    maintainer says "stop touching this", and the posting and the pushing are
    what it has to stop.
    """
    for pr in provider.list_open_prs(repo):
        if pr.number != number:
            continue
        if not forge.is_agent_pull_request(pr, repo, viewer):
            fail(f"{repo}#{number} is not one of this agent's pull requests.")
        if pr.is_ignored:
            fail(
                f"{repo}#{number} is labelled {forge.IGNORE_LABEL}, so the agent does not "
                "post on it. Nothing was posted."
            )
        return pr
    fail(f"{repo}#{number} is not an open pull request.")


def confined_body(path: str) -> str:
    """A model-written comment body, read from a path confined to scratch.

    Symlinks are resolved before the prefix check, so a link planted inside
    scratch cannot reach outside it.
    """
    scratch = os.path.realpath(SCRATCH_DIR)
    real = os.path.realpath(path)
    if not real.startswith(scratch + os.sep):
        fail(f"Reply body {path} resolves outside {scratch}.")
    if not os.path.isfile(real):
        fail(f"Reply body {path} does not exist.")
    with open(real, "r", encoding="utf-8") as handle:
        body = handle.read()
    if not body.strip():
        fail(f"Reply body {path} is empty.")
    return body


def post_body(provider, repo: str, pr, body: str) -> None:
    """Post `body` on `pr`.

    A one-line delegation, kept as a named function because what it does not do
    is the point — `github_scan_gate._post_body` is the same shape for the same
    reason. It used to stage the body in a temporary file inside `SCRATCH_DIR`,
    on the volume the credential sidecar also mounted. #913 split the shell and
    the broker into separate pods, so there is no longer a filesystem both sides
    can see, and `post_comment` takes the text and sends it on fd 0.

    `confined_body` above still bounds where the *input* may come from. That
    confinement is about what the model is allowed to read and is unaffected by
    how the result travels, which is why `SCRATCH_DIR` outlives this function's
    use of it.
    """
    provider.post_comment(repo, pr, body)
