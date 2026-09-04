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
import tempfile

import forge

#: The one directory a comment body may be read from, and the one a stamped
#: copy is written to. Bodies posted from here are public, so the path is
#: bounded rather than merely checked for existence.
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
    """`repo` as `owner/name` and on the managed allowlist, or `ValueError`.

    Two checks rather than one. The slug check is what stops a value that
    reaches a `gh` argument list from carrying path traversal or a leading
    dash; the allowlist is what stops a hand-run or a stale card aiming a
    write at a repository the install was never given.

    `gitops_workspace` is imported here rather than at module scope because
    this module is force-synced into `$HERMES_HOME/scripts` alongside it, and
    a top-level import would make every consumer of `pr_skill` pay for that
    module's own imports.
    """
    from gitops_workspace import get_managed_github_repos, is_valid_repo_slug

    if not repo or not is_valid_repo_slug(repo):
        raise ValueError(f"Invalid repository format: {repo!r}. Expected 'owner/name'.")
    managed = get_managed_github_repos()
    if managed and repo not in managed:
        raise ValueError(
            f"Repository {repo!r} is not in the managed repositories list: {managed}"
        )
    return repo


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
    """Post `body` on `pr`, via a temporary file inside the scratch directory.

    Through a file rather than argv because the body carries a reviewer's own
    words — or a CI log excerpt — back onto the forge and can run to thousands
    of characters, with the quoting rules of two shells and a proxy in between.
    The temporary copy lands in the same confined directory the input came
    from, and is removed whether or not the post succeeded.
    """
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", dir=SCRATCH_DIR, delete=False
    )
    try:
        handle.write(body)
        handle.close()
        provider.post_comment(repo, pr, handle.name)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
