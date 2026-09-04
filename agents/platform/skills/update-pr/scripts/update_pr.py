#!/usr/bin/env python3
"""update_pr.py — deterministic helper for the update-pr skill.

Two subcommands:

* ``poll`` — print the state of the agent's own open pull requests as JSON:
  whether each conflicts with its base, which checks are red on its head
  commit, whether that head has already been worked, and how much of the
  attempt budget is left. The `github-repo-watcher` cron job runs the same
  reads to decide whether to file a card; this exposes them so the worker can
  re-read the truth before it acts, and so a human debugging a card that never
  arrived can run the exact thing the watcher ran.
* ``record`` — post a summary comment and stamp it with the marker that records
  one update attempt against a head commit.

Why the marker exists, and why this command writes it
-----------------------------------------------------
Every other loop in this repository is anchored to something a human did: a
comment is answered once, and the marker naming that comment closes it. An
update run has no such anchor. It is triggered by the *state of the branch* —
conflicted, or red — and its own fix commit changes that state. A fix that does
not work therefore re-triggers the sweep on a new head sha, and the agent tries
again, on a pull request nobody asked it to touch.

Two bounds stop that, and both are read from the thread rather than tracked in
a database, because the sweep and the worker are separate processes on separate
schedules and the thread is the only state both of them can see:

* One attempt per head commit. ``<!-- agent-updated:<sha> -->`` in a
  self-authored comment means "this tip has been worked", whatever the outcome,
  so a run that could not fix anything is not repeated every ten minutes.
* ``PR_AGENT_MAX_UPDATE_ATTEMPTS`` markers in total on one pull request. This
  is what makes the loop terminate rather than merely slow down: without it, a
  fix that pushes a commit and does not work mints a fresh tip each time and
  the per-tip bound never binds.

If the model had to type the marker, forgetting it would not produce a missing
comment — it would produce an unbounded fix loop. So it is appended here, from
``--attempted-sha``, and that sha is resolved against the pull request's own
commits first: a mistyped one produces a marker matching nothing, which is the
same runaway by a slower road.

Both bounds count markers, so they only bind on a run that reaches this
command. That is why every refusal below, on a run whose commits are already on
the branch, posts the marker before it exits rather than leaving the thread
untouched: a pushed branch with no marker spends nothing from the budget while
minting a fresh tip, and the sweep hands it straight back. "Every" is checked
by a test that walks the refusals, because the guarantee is worth exactly as
much as its least-travelled path — an unresolvable ``--pushed`` used to exit
without a marker, which is the likeliest way to reach one of these at all.

Two things it does not cover. Resolving ``--attempted-sha`` itself comes first
and refuses plainly, because it is what "already on the branch" would be
measured against; nothing has been posted by then and nothing about the thread
has changed, so the next tick simply cards the pull request again. The real
residue is a turn that dies before ``record`` is invoked at all — a reaped
turn, a crashed container. Those leave a pushed branch unmarked, and the loop
is bounded there only by whoever notices. Closing it would mean writing the
marker before the work rather than after, which trades this bound against the
one ``pr_triggers.updated_head_shas`` chose deliberately: that a crashed turn
must not park a pull request for good with nothing said to anyone.

``record`` also refuses to post a claim it cannot check. ``--pushed`` names a
commit the run made, and it must be on the pull request and must come after the
tip the run started from; ``--no-change`` says the branch was not touched. One
of the two is required, for the reason ``pr_conversation.reply`` requires the
same pair: a comment saying the conflict was resolved, on a branch that still
conflicts, is worse than no comment at all.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# `$HERMES_HOME/scripts`, where the entrypoint's step 2b force-sync stages the
# shared modules. Resolved from the environment rather than by walking up from
# __file__, because the skill directory is a symlink into the profile home and
# the relative path from there is not the path on disk.
_SCRIPTS = os.path.join(os.environ.get("HERMES_HOME", "/opt/data"), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import forge  # noqa: E402
import pr_skill  # noqa: E402
import pr_triggers  # noqa: E402

#: Shortest sha abbreviation `--attempted-sha` and `--pushed` will accept. Git's
#: own default. Both are resolved to the full sha against the pull request's
#: commits before anything is written, so the abbreviation is a convenience for
#: whatever the model copied out of git output rather than what lands in the
#: marker.
SHA_MIN_LEN = 7

#: How much of a check's name and details URL travel into the JSON. Both come
#: from whatever reported the check, so both are third-party text on its way
#: into a prompt. Taken from `forge`, which owns `CheckRun`, so a card and a
#: `poll` row cannot end up showing the model different amounts of one name.
MAX_CHECK_NAME_CHARS = forge.MAX_CHECK_NAME_CHARS
MAX_CHECK_URL_CHARS = forge.MAX_CHECK_URL_CHARS

#: How many failing checks one row lists. A pull request with fifty red checks
#: has one cause, not fifty, and the skill re-reads CI itself at stage 3 — but
#: an uncapped list is an uncapped amount of third-party text, and the count is
#: reported alongside so the model can tell a short list from a truncated one.
MAX_CHECKS_IN_ROW = 10


def _survey(provider, repo: str, pr, viewer: str, budget: int) -> dict:
    """One pull request's health, its attempt history, and what to do about it.

    The status field is the whole point: the worker should not have to derive
    "there is nothing to do here" from a conflict flag and an empty list, and
    the two stopping conditions it must honour — this tip already worked, this
    pull request's budget spent — are not visible in the health reads at all.
    """
    conflicted = provider.conflict_state(repo, pr)
    failing = provider.failing_checks(repo, pr)
    attempted = pr_triggers.updated_head_shas(provider.list_comments(repo, pr), viewer)

    if not conflicted and not failing:
        # `conflicted` is None while the forge is still computing the merge, and
        # that is not the same as "clean" — but with nothing red either there is
        # no work to hand over yet, and the next sweep re-reads in ten minutes.
        status = "HEALTHY" if conflicted is False else "INDETERMINATE"
    elif not pr.head_sha:
        # The same refusal `sweep_pr_updates` makes, and for the same reason:
        # every bound here is keyed on the tip, so a pull request the forge gave
        # no head sha for cannot be attempted safely. `record` would reject the
        # empty sha at the end of the run, after the commits were already
        # pushed. The sweep filters these out before they reach a card, so this
        # is the hand-run path — which is exactly the one with no sweep in front
        # of it.
        status = "UNREADABLE"
    elif pr.head_sha in attempted:
        status = "ALREADY_ATTEMPTED"
    elif len(attempted) >= budget:
        status = "BUDGET_SPENT"
    else:
        status = "FOUND"

    return {
        "status": status,
        # On the row rather than only on the envelope: one poll now spans every
        # managed repository, and `record` needs `--repo` as well as `--pr`, so
        # a row that named only the number would send the worker looking for the
        # repository it came from.
        "repository": repo,
        "pr": pr.number,
        "head_ref": pr.head_ref,
        "base_ref": pr.base_ref,
        "head_sha": pr.head_sha,
        "conflicted": conflicted,
        "failing_checks": [
            {
                "name": check.name[:MAX_CHECK_NAME_CHARS],
                "name_truncated_chars": max(
                    0, len(check.name) - MAX_CHECK_NAME_CHARS
                ),
                "conclusion": check.conclusion,
                "details_url": check.details_url[:MAX_CHECK_URL_CHARS],
                "register": check.register,
            }
            for check in failing[:MAX_CHECKS_IN_ROW]
        ],
        # Reported rather than left to be inferred from the list length: a
        # silent truncation reads as "these are all of them", and the stage-3
        # instruction is to fix the cause rather than the checks it was handed.
        "failing_checks_total": len(failing),
        "failing_checks_omitted": max(0, len(failing) - MAX_CHECKS_IN_ROW),
        "attempts_used": len(attempted),
        "attempts_allowed": budget,
    }


def handle_poll(args) -> int:
    """Print the agent's unmergeable pull requests as JSON.

    Deliberately mirrors `pr_conversation poll`'s status vocabulary
    (`NOT_CONFIGURED` / `ERROR` / `FOUND`, with `NO_WORK` where that one says
    `NO_REQUESTS`) so one operator-facing glossary covers the whole watcher.
    """
    try:
        from gitops_workspace import get_managed_github_repos

        if args.repo:
            repos = [pr_skill.validate_repo(args.repo)]
        else:
            repos = get_managed_github_repos()
    except ValueError as error:
        print(
            json.dumps(
                {"status": "ERROR", "reason": "INVALID_REPOSITORY", "value": str(error)}
            )
        )
        return 0
    except forge.ForgeError as error:
        print(
            json.dumps({"status": "ERROR", "reason": error.reason, "value": error.value})
        )
        return 0
    except Exception as error:
        print(
            json.dumps(
                {"status": "ERROR", "reason": "DISCOVERY_FAILED", "value": str(error)}
            )
        )
        return 0
    if not repos:
        print(json.dumps({"status": "NOT_CONFIGURED"}))
        return 0

    budget = pr_triggers.max_update_attempts()
    provider = forge.provider_for(repo=repos[0])
    rows = []
    try:
        provider.preflight()
        viewer = provider.viewer_login()
        if not viewer:
            print(
                json.dumps({"status": "ERROR", "reason": "VIEWER_UNKNOWN", "value": ""})
            )
            return 0
        for repo in repos:
            for pr in provider.list_open_prs(repo):
                if not forge.is_agent_pull_request(pr, repo, viewer) or pr.is_ignored:
                    continue
                if args.pr and pr.number != args.pr:
                    continue
                rows.append(_survey(provider, repo, pr, viewer, budget))
        if args.pr and not rows:
            # Distinguished from `NO_WORK`, because the two mean opposite
            # things to a worker holding a card: nothing to do here, versus
            # this is not a pull request you may touch and the card is
            # stale, withdrawn, or aimed at somebody else's branch.
            print(
                json.dumps(
                    {
                        "status": "NOT_FOUND",
                        "repositories": repos,
                        "pr": args.pr,
                    }
                )
            )
            return 0
    except forge.ForgeError as error:
        print(
            json.dumps({"status": "ERROR", "reason": error.reason, "value": error.value})
        )
        return 0

    actionable = [row for row in rows if row["status"] == "FOUND"]
    print(
        json.dumps(
            {
                "status": "FOUND" if actionable else "NO_WORK",
                "repositories": repos,
                "pull_requests": rows,
            }
        )
    )
    return 0


def _resolve_sha(commits, value: str, label: str, on_fail=None) -> tuple[str, int]:
    """The full sha and branch position of `value`, or exit.

    Resolved against the pull request's own commits rather than accepted as
    typed. An abbreviation is fine — it is what `git log` prints — but the
    marker has to carry the full sha, because the sweep compares it against
    `head_sha` by exact string equality and a short one would match nothing.

    `on_fail` is how a caller that has more to do before exiting takes the
    refusal over. `handle_record` passes its `refuse`, because a `--pushed`
    that will not resolve is one of the ways a run reaches an exit with the
    branch already moved, and a moved branch owes the thread a marker whatever
    the reason. Default is a plain exit, which is right for `--attempted-sha`:
    it is resolved before there is an anchor to compute "moved" against.
    """
    fail = on_fail or pr_skill.fail
    text = str(value or "").strip().lower()
    if len(text) < SHA_MIN_LEN:
        fail(f"{label} {value!r} is shorter than {SHA_MIN_LEN} characters.")
    matches = [
        (index, commit.sha)
        for index, commit in enumerate(commits)
        if commit.sha.lower().startswith(text)
    ]
    if not matches:
        fail(f"{label} {value} is not a commit on this pull request.")
    if len(matches) > 1:
        fail(
            f"{label} {value} matches {len(matches)} commits on this pull "
            "request. Give more characters."
        )
    index, sha = matches[0]
    return sha, index


def handle_record(args) -> int:
    repo = pr_skill.resolve_repo(args)
    provider = forge.provider_for(repo=repo)

    # Everything that talks to the forge before the post, inside one guard, so
    # an auth blip reaches the model as a reason code rather than as a Python
    # traceback after it has already written the body.
    try:
        provider.preflight()
        viewer = provider.viewer_login()
        if not viewer:
            pr_skill.fail(
                "the GitHub credential could not name the account it authenticates as."
            )
        pr = pr_skill.find_agent_pr(provider, repo, args.pr, viewer)
        comments = provider.list_comments(repo, pr)
        commits = provider.list_commits(repo, pr)
    except forge.ForgeError as error:
        pr_skill.fail(f"{error.reason}: {error.value}")

    # No `on_fail`: this is the one resolution with nothing to fall back on. It
    # is what "moved" would be measured from, so a run that cannot resolve it
    # cannot know whether the branch moved, and the plain exit is honest —
    # nothing has been posted, and nothing about the thread has changed.
    attempted_sha, attempted_at = _resolve_sha(
        commits,
        args.attempted_sha,
        "--attempted-sha",
        on_fail=lambda message: pr_skill.fail(f"{message} Nothing was posted."),
    )

    # Counted from the thread this call just read rather than from what `poll`
    # saw: between the two, another worker may have recorded an attempt.
    already = pr_triggers.updated_head_shas(comments, viewer)
    if attempted_sha in already:
        # The one refusal below that does not go through `refuse`: the marker
        # this run owes is already on the thread, so the tip is marked and the
        # attempt is counted whatever else is wrong with the call.
        pr_skill.fail(
            f"an update attempt against {attempted_sha[:SHA_MIN_LEN]} is already "
            f"recorded on {repo}#{args.pr}. Nothing was posted."
        )

    # Whether this run has already moved the branch, which is what makes every
    # refusal below dangerous rather than merely unhelpful.
    #
    # Both bounds on this loop are counted off `agent-updated` markers, and
    # `record` is the only writer of one. So a run that pushed and then exited
    # here leaves the branch moved and the thread unchanged: the new tip is not
    # in `updated_head_shas`, `len()` of that set has not grown, and
    # `_update_card`'s idempotency key carries the head sha, so it mints afresh
    # rather than waiting out its hour. None of the three binds, the next tick
    # cards the pull request again, and `pr_comments` skips it again because
    # `pr_updates` claims what it cards — so the reviewer waiting on that
    # branch is never answered either.
    #
    # A pushed branch therefore gets its marker whatever else is wrong with the
    # call. That is what §4 of the design means by "a run that could not fix
    # what it found still writes one, so it is not repeated every ten minutes".
    branch_moved = attempted_at < len(commits) - 1

    def refuse(message: str):
        """Fail — marking the tip first, if the branch has already moved."""
        if branch_moved:
            # "Commits landed", not "I pushed": `branch_moved` says the branch
            # is ahead of `--attempted-sha`, and on the paths that reach here
            # by way of a wrong `--attempted-sha` that is a commit from an
            # earlier run rather than this one. Marking it anyway is the
            # conservative reading — it spends one attempt on a run that may
            # have done nothing, which stops after five, where the alternative
            # does not stop at all.
            note = (
                f"Commits have landed on `{pr.head_ref}` since "
                f"`{attempted_sha[:SHA_MIN_LEN]}`, and I could not record the "
                f"attempt against it: {message}\n\n"
                "Nothing has been reverted. That tip is marked as attempted, "
                "so it counts against the update budget and the sweep will "
                "not hand the branch straight back. Somebody should read what "
                "landed."
            )
            try:
                pr_skill.post_body(
                    provider,
                    repo,
                    pr,
                    f"{note}\n\n"
                    f"{pr_triggers.marker(attempted_sha, pr_triggers.UPDATED_MARKER)}\n",
                )
            except forge.ForgeError as error:
                pr_skill.fail(
                    f"{message} The attempt could not be marked either "
                    f"({error}), so this pull request will be carded again."
                )
            pr_skill.fail(f"{message} The attempt was marked; see the thread.")
        pr_skill.fail(f"{message} Nothing was posted.")

    pushed = []
    positions = set()
    for value in args.pushed or []:
        # Through `refuse`, because a `--pushed` that will not resolve is the
        # likeliest way to get here on a branch that has already moved: the
        # commits are on the branch and the argument naming them is mistyped or
        # too short. Resolving it with a plain exit would leave the tip
        # unmarked, which is the unbounded loop this whole block exists to
        # close, and `SKILL.md` tells the model not to retry after a refusal.
        sha, position = _resolve_sha(commits, value, "--pushed", on_fail=refuse)
        if position <= attempted_at:
            # Every commit the agent ever made is on this branch, including the
            # one that opened the pull request, so membership alone would pass
            # for a commit that predates the run entirely. What makes a commit
            # this run's work is that it landed after the tip the run started
            # from.
            refuse(
                f"--pushed {value} is not newer than the tip this run started "
                f"from ({attempted_sha[:SHA_MIN_LEN]}), so it is not a commit "
                "this run made."
            )
        positions.add(position)
        pushed.append(sha)

    # `--attempted-sha` has to be the tip the run *started from*, and the check
    # above only proves it is somewhere behind the commits being claimed. That
    # is not enough on its own: the marker is compared against `head_sha` by
    # exact equality, so naming an older commit — the one that opened the pull
    # request, say, which SKILL.md warns against twice because it is the easy
    # mistake — writes a marker the sweep never matches. The per-tip bound then
    # does not bind at all and only the total budget stops the loop: five worker
    # turns and five public comments within the hour instead of five over the
    # branch's life.
    #
    # The invariant that pins it exactly: every commit after the attempted sha
    # must be one this run is claiming. Nothing else may have landed in between.
    undeclared = [
        commits[index].sha[:SHA_MIN_LEN]
        for index in range(attempted_at + 1, len(commits))
        if index not in positions
    ]
    if undeclared:
        refuse(
            f"--attempted-sha {attempted_sha[:SHA_MIN_LEN]} is not the tip this "
            f"run started from: {', '.join(undeclared)} came after it and is not "
            "in --pushed. Name the tip `poll` reported as `head_sha`, and pass "
            "every commit this run made."
        )

    # Marker syntax is stripped out of the model's body before the real marker
    # is appended. `updated_head_shas` reads raw bodies and counts every marker
    # in a self-authored comment, so one the model imitated in its own prose
    # becomes a real marker the moment this posts — and a marker naming another
    # sha would record an attempt that never happened, spending the budget of a
    # pull request nobody has looked at. The model holds both halves: SKILL.md
    # prints the syntax in full in order to forbid it, and `poll` carries every
    # sha. A line of prose is not the boundary that belongs in front of that.
    #
    # `confined_body` refuses by calling `pr_skill.fail`, which exits — and its
    # docstring promises that nothing has been written by the time the model
    # reads the error. On a run that has already pushed, that promise is the
    # bug: the write happened before `record` was invoked at all. Catching the
    # exit is how the refusal reaches `refuse`, which marks the tip and then
    # fails the same way. The specific reason is already on stderr by then.
    try:
        raw = pr_skill.confined_body(args.body_file)
    except SystemExit:
        refuse(f"the comment body {args.body_file} could not be read (see above).")
    body = pr_triggers.strip_markers(raw)
    if not body:
        refuse(f"Comment body {args.body_file} is nothing but marker syntax.")
    stamped = (
        f"{body}\n\n{pr_triggers.marker(attempted_sha, pr_triggers.UPDATED_MARKER)}\n"
    )

    try:
        pr_skill.post_body(provider, repo, pr, stamped)
    except forge.ForgeError as error:
        pr_skill.fail(f"could not post to {repo}#{args.pr}: {error}")

    print(
        json.dumps(
            {
                "status": "RECORDED",
                "repository": repo,
                "pr": args.pr,
                "attempted_sha": attempted_sha,
                "pushed": pushed,
                "attempts_used": len(already) + 1,
                "attempts_allowed": pr_triggers.max_update_attempts(),
            }
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="report pull-request health as JSON")
    poll.add_argument(
        "--repo",
        default=None,
        help="limit to one managed repository (owner/repo); every managed "
        "repository is swept when omitted",
    )
    poll.add_argument("--pr", type=int, default=0, help="limit to one pull request")
    poll.set_defaults(func=handle_poll)

    record = sub.add_parser(
        "record", help="post a summary and mark this head commit as attempted"
    )
    record.add_argument(
        "--repo",
        default=None,
        required=True,
        help="the repository the pull request is on (owner/repo)",
    )
    record.add_argument("--pr", type=int, required=True)
    record.add_argument(
        "--attempted-sha",
        required=True,
        help="the head commit this run started from, which the marker records",
    )
    record.add_argument(
        "--body-file",
        required=True,
        help=f"path to the comment body, under {pr_skill.SCRATCH_DIR}",
    )
    # Required and exclusive: a run either changed the branch or it did not, and
    # saying which is what the commit checks above verify.
    claim = record.add_mutually_exclusive_group(required=True)
    claim.add_argument(
        "--pushed",
        action="append",
        metavar="SHA",
        help="a commit this run pushed; repeat once per stage. Each is checked "
        "against the pull request before anything is posted",
    )
    claim.add_argument(
        "--no-change",
        dest="no_change",
        action="store_true",
        help="this run pushed nothing",
    )
    record.set_defaults(func=handle_record)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
