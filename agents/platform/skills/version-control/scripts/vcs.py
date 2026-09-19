#!/usr/bin/env python3
"""Version control from a container that holds no credential.

Everything that needs the token happens somewhere else. This script talks to
exactly two things: the sandbox's own git, against a local working copy with no
remote, and `POST /v1/vcs/*` on the credential broker over loopback. There is no
third case. No verb here shells out to a network client, none of them names
GitHub, and the local git has no HTTP transport and no ssh client to exec, so
nothing this script runs can present a request to a forge.

The shape is symmetric. `clone` asks the broker for a git bundle and unpacks it;
`publish` bundles the revisions made since that clone and hands them back.
History is objects and refs in both directions — no `.git/config`, no hooks, no
remote URL — so the local copy is a real repository that answers every question
about the past at full fidelity, and the broker never checks out anything the
sandbox produced.

Between those two calls, everything is local. `commit` runs the sandbox's git.
`log`, `show`, `diff`, `annotate`, `files`, `grep` and `status` run the sandbox's
git. A change is a real revision with a real parent before it goes anywhere, so
a branch of five commits arrives as five commits with the same identifiers on
both sides.

The verbs are the version-control concepts rather than one system's spelling of
them. Where systems disagree the neutral name is the command and the familiar
one is an alias — `annotate`/`blame`, `publish`/`push`, `proposal`/`pr`/`mr`.
`docs/designs/version-control-support.md` §2 records the sources that
vocabulary was drawn from.

Every subcommand prints one JSON object on stdout.

    vcs.py clone https://github.com/acme/infra
    vcs.py log -n 20 -- inventory/clusters.yaml
    vcs.py annotate scripts/rotate-keys.sh
    vcs.py branch fix/replicas
    vcs.py commit inventory/clusters.yaml -m 'raise replicas to 5'
    vcs.py publish
    vcs.py proposal create --title 'Raise replicas' --body 'Evening peak headroom.'
    vcs.py issue list --state open --labels bug
    vcs.py discard
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

import credential_proxy_client  # noqa: E402
import vcs_client as client  # noqa: E402
from vcs_client import VcsError  # noqa: E402


# ---- repository verbs -----------------------------------------------------


def verb_capabilities(arguments) -> dict:
    spec = arguments.repository or (arguments.repo if arguments.repo else None)
    if not spec:
        spec = client.resolve_session(None)["spec"]
    answer = client.call("capabilities", {"repository": spec})
    answer["localGit"] = client.LOCAL_GIT if Path(client.LOCAL_GIT).exists() else None
    return answer


def verb_clone(arguments) -> dict:
    return client.clone(arguments.repository, arguments.branch, force=arguments.force)


def verb_branch(arguments) -> dict:
    return client.branch(arguments.repo, arguments.name)


def verb_commit(arguments) -> dict:
    return client.commit(arguments.message, arguments.paths, spec=arguments.repo)


def verb_publish(arguments) -> dict:
    return client.publish(arguments.repo, arguments.target, advance=arguments.advance)


def verb_discard(arguments) -> dict:
    return client.discard(arguments.repo)


def _collaboration(arguments, verb: str, payload: dict) -> dict:
    return client.forge(verb, payload, arguments.repo)


def verb_log(arguments) -> dict:
    session = client.resolve_session(arguments.repo)
    args = ["log", f"--max-count={arguments.limit}", "--date=iso"]
    # `--format` carries the format string, not a whole git option, which is
    # what its help text promises and what anybody typing `--format "%h %s"`
    # means. Appended raw it becomes a positional argument to `git log`, and
    # git reads it as a revision: "ambiguous argument '%h %s'".
    args.append(
        f"--pretty=format:{arguments.format}"
        if arguments.format
        else "--pretty=format:%H%x09%an%x09%ad%x09%s"
    )
    if arguments.patch:
        args.append("--patch")
    if arguments.revision:
        args.append(arguments.revision)
    if arguments.paths:
        args += ["--", *arguments.paths]
    return client.local(session, args, "log")


def verb_show(arguments) -> dict:
    session = client.resolve_session(arguments.repo)
    return client.local(session, ["show", arguments.revision], "show")


def verb_diff(arguments) -> dict:
    session = client.resolve_session(arguments.repo)
    args = ["diff"]
    if arguments.revision:
        args.append(arguments.revision)
    if arguments.paths:
        args += ["--", *arguments.paths]
    return client.local(session, args, "diff")


def verb_annotate(arguments) -> dict:
    session = client.resolve_session(arguments.repo)
    args = ["annotate", "--date=short"]
    if arguments.revision:
        args.append(arguments.revision)
    args += ["--", arguments.path]
    return client.local(session, args, "annotate")


def verb_files(arguments) -> dict:
    """The manifest: every tracked path with the mode the revision records.

    The mode is the point. Whether a script is executable is a property of the
    tree entry, and it is the one thing a protocol that carries only bytes has
    nowhere to put — which is why this verb exists rather than `ls`.
    """
    session = client.resolve_session(arguments.repo)
    args = ["ls-files", "--stage"]
    if arguments.paths:
        args += ["--", *arguments.paths]
    result = client.local(session, args, "files")
    entries = []
    for line in result["stdout"].splitlines():
        head, _, path = line.partition("\t")
        parts = head.split()
        if path and len(parts) >= 2:
            entries.append({"mode": parts[0], "revision": parts[1], "path": path})
    result["files"] = entries
    result["count"] = len(entries)
    del result["stdout"]
    return result


def verb_grep(arguments) -> dict:
    session = client.resolve_session(arguments.repo)
    args = ["grep", "--line-number"]
    if arguments.ignore_case:
        args.append("--ignore-case")
    args.append("--extended-regexp" if arguments.regex else "--fixed-strings")
    args += ["-e", arguments.pattern]
    if arguments.paths:
        args += ["--", *arguments.paths]
    result = client.local(session, args, "grep")
    # git grep exits 1 for "no match", which is an answer rather than a failure
    # and should not read to the caller as one.
    if result["exitCode"] == 1 and not result["stderr"]:
        result["exitCode"] = 0
        result["matches"] = 0
    else:
        result["matches"] = len(result["stdout"].splitlines())
    return result


def verb_status(arguments) -> dict:
    session = client.resolve_session(arguments.repo)
    result = client.local(session, ["status", "--porcelain=v1"], "status")
    result["changes"] = [
        {"state": line[:2].strip(), "path": line[3:]}
        for line in result["stdout"].splitlines()
        if line
    ]
    result["count"] = len(result["changes"])
    del result["stdout"]
    return result


def verb_proposal_create(arguments) -> dict:
    source, target = arguments.source, arguments.target
    if not source or not target:
        session = client.resolve_session(arguments.repo)
        source = source or client.current_branch(session)
        target = target or session["branch"]
    return _collaboration(
        arguments,
        "proposal-create",
        {
            "title": arguments.title,
            "body": arguments.body,
            "source": source,
            "target": target,
            "draft": arguments.draft or None,
        },
    )


def verb_proposal_list(arguments) -> dict:
    return _collaboration(
        arguments,
        "proposal-list",
        {
            "state": arguments.state,
            "limit": arguments.limit,
            "source": arguments.source,
            "target": arguments.target,
        },
    )


def verb_proposal_view(arguments) -> dict:
    return _collaboration(
        arguments,
        "proposal-view",
        {
            "number": arguments.number,
            "comments": arguments.comments or None,
            "diff": arguments.diff or None,
            "limit": arguments.limit,
        },
    )


def verb_proposal_comment(arguments) -> dict:
    return _collaboration(
        arguments,
        "proposal-comment",
        {"number": arguments.number, "body": arguments.body},
    )


def verb_issue_list(arguments) -> dict:
    return _collaboration(
        arguments,
        "issue-list",
        {
            "state": arguments.state,
            "limit": arguments.limit,
            "labels": arguments.labels or None,
            "excludeLabels": arguments.without_labels or None,
            "query": arguments.query or None,
        },
    )


def verb_issue_view(arguments) -> dict:
    return _collaboration(
        arguments,
        "issue-view",
        {
            "number": arguments.number,
            "comments": arguments.comments or None,
            "limit": arguments.limit,
        },
    )


def verb_issue_create(arguments) -> dict:
    return _collaboration(
        arguments,
        "issue-create",
        {
            "title": arguments.title,
            "body": arguments.body,
            "labels": arguments.labels or None,
        },
    )


def verb_issue_comment(arguments) -> dict:
    return _collaboration(
        arguments,
        "issue-comment",
        {"number": arguments.number, "body": arguments.body},
    )


def verb_proposal_update(arguments) -> dict:
    return _collaboration(
        arguments,
        "proposal-update",
        {
            "number": arguments.number,
            "title": arguments.title,
            "body": arguments.body,
            "labelsAdd": arguments.add_label or None,
            "labelsRemove": arguments.remove_label or None,
        },
    )


def verb_proposal_close(arguments) -> dict:
    return _collaboration(arguments, "proposal-close", {"number": arguments.number})


def verb_proposal_commits(arguments) -> dict:
    return _collaboration(
        arguments,
        "proposal-commits",
        {"number": arguments.number, "limit": arguments.limit},
    )


def verb_proposal_acknowledge(arguments) -> dict:
    return _collaboration(
        arguments,
        "proposal-acknowledge",
        {
            "number": arguments.number,
            "comment": {"id": arguments.comment_id, "kind": arguments.kind},
        },
    )


def verb_issue_update(arguments) -> dict:
    return _collaboration(
        arguments,
        "issue-update",
        {
            "number": arguments.number,
            "title": arguments.title,
            "body": arguments.body,
            "labelsAdd": arguments.add_label or None,
            "labelsRemove": arguments.remove_label or None,
        },
    )


def verb_issue_close(arguments) -> dict:
    return _collaboration(
        arguments,
        "issue-close",
        {"number": arguments.number, "reason": arguments.reason},
    )


def verb_label_ensure(arguments) -> dict:
    return _collaboration(
        arguments,
        "label-ensure",
        {
            "name": arguments.name,
            "color": arguments.color,
            "description": arguments.description,
        },
    )


def verb_identity(arguments) -> dict:
    return _collaboration(arguments, "identity", {"login": arguments.login})


# ---- command line ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vcs.py", description=__doc__.splitlines()[0]
    )
    verbs = parser.add_subparsers(dest="verb", required=True)

    def repo_option(sub):
        sub.add_argument(
            "--repo",
            help="which repository, when more than one is cloned locally or the "
            "current directory is not inside one",
        )
        return sub

    caps = verbs.add_parser("capabilities", help="what this install can do")
    caps.add_argument("repository", nargs="?", help="repository URL or owner/name")
    repo_option(caps).set_defaults(run=verb_capabilities)

    clone = verbs.add_parser("clone", help="local copy, with full history")
    clone.add_argument("repository", help="repository URL or owner/name")
    clone.add_argument("--branch", help="which line of development (default: the trunk)")
    clone.add_argument(
        "--force",
        action="store_true",
        help="replace an existing copy even if it holds unpublished work",
    )
    clone.set_defaults(run=verb_clone)

    log = verbs.add_parser("log", aliases=["history"], help="the revisions behind HEAD")
    log.add_argument("-n", "--limit", type=int, default=20)
    log.add_argument("--revision", help="start from this revision or branch")
    log.add_argument("--format", help="a pretty format string, e.g. '%%h %%s'")
    log.add_argument("--patch", action="store_true", help="include the diffs")
    log.add_argument("paths", nargs="*", help="restrict to these paths")
    repo_option(log).set_defaults(run=verb_log)

    show = verbs.add_parser("show", help="one revision, or a file as of one")
    show.add_argument("revision", help="a revision, or revision:path")
    repo_option(show).set_defaults(run=verb_show)

    diff = verbs.add_parser("diff", help="differences in the working copy")
    diff.add_argument("--revision", help="compare against this revision instead")
    diff.add_argument("paths", nargs="*")
    repo_option(diff).set_defaults(run=verb_diff)

    annotate = verbs.add_parser(
        "annotate", aliases=["blame"], help="per-line last-change attribution"
    )
    annotate.add_argument("path")
    annotate.add_argument("--revision", help="as of this revision")
    repo_option(annotate).set_defaults(run=verb_annotate)

    files = verbs.add_parser(
        "files", aliases=["manifest"], help="tracked paths and their modes"
    )
    files.add_argument("paths", nargs="*")
    repo_option(files).set_defaults(run=verb_files)

    grep = verbs.add_parser("grep", aliases=["search"], help="find text in the copy")
    grep.add_argument("pattern")
    grep.add_argument("paths", nargs="*")
    grep.add_argument("--regex", action="store_true")
    grep.add_argument("-i", "--ignore-case", action="store_true")
    repo_option(grep).set_defaults(run=verb_grep)

    status = verbs.add_parser("status", help="what the copy has that HEAD does not")
    repo_option(status).set_defaults(run=verb_status)

    branch = verbs.add_parser("branch", help="list lines of development, or start one")
    branch.add_argument("name", nargs="?", help="the branch to create or switch to")
    repo_option(branch).set_defaults(run=verb_branch)

    commit = verbs.add_parser("commit", help="record a revision locally")
    commit.add_argument("paths", nargs="*", help="default: everything that changed")
    commit.add_argument("-m", "--message", required=True)
    repo_option(commit).set_defaults(run=verb_commit)

    publish = verbs.add_parser(
        "publish", aliases=["push"], help="send local revisions to the forge"
    )
    publish.add_argument("--target", help="the branch to build on (default: cloned)")
    publish.add_argument(
        "--advance",
        action="store_true",
        help="this copy was cloned of a proposal branch to add to it; needs --target",
    )
    repo_option(publish).set_defaults(run=verb_publish)

    discard = verbs.add_parser(
        "discard", aliases=["close"], help="remove the local copy"
    )
    repo_option(discard).set_defaults(run=verb_discard)

    proposal = verbs.add_parser(
        "proposal", aliases=["pr", "mr"], help="change proposals on the forge"
    )
    actions = proposal.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create", aliases=["open"])
    create.add_argument("--title", required=True)
    create.add_argument("--body", default="")
    create.add_argument("--source", help="the branch to merge (default: current)")
    create.add_argument("--target", help="the branch to merge into (default: cloned)")
    create.add_argument("--draft", action="store_true")
    repo_option(create).set_defaults(run=verb_proposal_create)

    plist = actions.add_parser("list")
    plist.add_argument("--state", default="open", choices=["open", "closed", "all"])
    plist.add_argument("--source", help="only proposals from this branch")
    plist.add_argument("--target", help="only proposals onto this branch")
    plist.add_argument("-n", "--limit", type=int)
    repo_option(plist).set_defaults(run=verb_proposal_list)

    pview = actions.add_parser("view")
    pview.add_argument("number", type=int)
    pview.add_argument("--comments", action="store_true")
    pview.add_argument("--diff", action="store_true")
    pview.add_argument("-n", "--limit", type=int)
    repo_option(pview).set_defaults(run=verb_proposal_view)

    pcomment = actions.add_parser("comment")
    pcomment.add_argument("number", type=int)
    pcomment.add_argument("--body", required=True)
    repo_option(pcomment).set_defaults(run=verb_proposal_comment)

    pupdate = actions.add_parser("update", aliases=["edit"])
    pupdate.add_argument("number", type=int)
    pupdate.add_argument("--title")
    pupdate.add_argument("--body")
    pupdate.add_argument("--add-label", nargs="*")
    pupdate.add_argument("--remove-label", nargs="*")
    repo_option(pupdate).set_defaults(run=verb_proposal_update)

    pclose = actions.add_parser("close")
    pclose.add_argument("number", type=int)
    repo_option(pclose).set_defaults(run=verb_proposal_close)

    pcommits = actions.add_parser("commits", help="the revisions on a proposal's source branch")
    pcommits.add_argument("number", type=int)
    pcommits.add_argument("-n", "--limit", type=int)
    repo_option(pcommits).set_defaults(run=verb_proposal_commits)

    pack = actions.add_parser(
        "acknowledge", aliases=["ack"], help="react to a comment so its author sees it was read"
    )
    pack.add_argument("number", type=int)
    pack.add_argument("--comment-id", type=int, required=True, help="the comment's `id` from `view --comments`")
    pack.add_argument("--kind", required=True, help="the comment's `kind` from `view --comments`")
    repo_option(pack).set_defaults(run=verb_proposal_acknowledge)

    issue = verbs.add_parser("issue", help="work items on the forge")
    iactions = issue.add_subparsers(dest="action", required=True)

    ilist = iactions.add_parser("list")
    ilist.add_argument("--state", default="open", choices=["open", "closed", "all"])
    ilist.add_argument("--labels", nargs="*")
    ilist.add_argument(
        "--without-labels", nargs="*", help="skip issues carrying any of these"
    )
    ilist.add_argument("--query", help="free text to search for")
    ilist.add_argument("-n", "--limit", type=int)
    repo_option(ilist).set_defaults(run=verb_issue_list)

    iview = iactions.add_parser("view")
    iview.add_argument("number", type=int)
    iview.add_argument("--comments", action="store_true")
    iview.add_argument("-n", "--limit", type=int)
    repo_option(iview).set_defaults(run=verb_issue_view)

    icreate = iactions.add_parser("create", aliases=["open"])
    icreate.add_argument("--title", required=True)
    icreate.add_argument("--body", default="")
    icreate.add_argument("--labels", nargs="*")
    repo_option(icreate).set_defaults(run=verb_issue_create)

    icomment = iactions.add_parser("comment")
    icomment.add_argument("number", type=int)
    icomment.add_argument("--body", required=True)
    repo_option(icomment).set_defaults(run=verb_issue_comment)

    iupdate = iactions.add_parser("update", aliases=["edit"])
    iupdate.add_argument("number", type=int)
    iupdate.add_argument("--title")
    iupdate.add_argument("--body")
    iupdate.add_argument("--add-label", nargs="*")
    iupdate.add_argument("--remove-label", nargs="*")
    repo_option(iupdate).set_defaults(run=verb_issue_update)

    iclose = iactions.add_parser("close")
    iclose.add_argument("number", type=int)
    iclose.add_argument("--reason", choices=["completed", "not-planned"])
    repo_option(iclose).set_defaults(run=verb_issue_close)

    label = verbs.add_parser("label", help="labels on the forge")
    lactions = label.add_subparsers(dest="action", required=True)
    lensure = lactions.add_parser("ensure", aliases=["create"], help="create the label, or update it if it exists")
    lensure.add_argument("name")
    lensure.add_argument("--color")
    lensure.add_argument("--description")
    repo_option(lensure).set_defaults(run=verb_label_ensure)

    identity = verbs.add_parser(
        "identity", aliases=["whoami"], help="who this install is on the forge, and whether a login may write"
    )
    identity.add_argument("--login", help="ask about this login instead of the credential's own")
    repo_option(identity).set_defaults(run=verb_identity)

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    client.ROOT.mkdir(parents=True, exist_ok=True)
    try:
        answer = arguments.run(arguments)
    except VcsError as exc:
        print(json.dumps(exc.as_json(), indent=2))
        return 1
    except subprocess.TimeoutExpired:
        print(json.dumps({"error": "the local git command timed out"}, indent=2))
        return 1
    except subprocess.CalledProcessError as exc:
        # Every other exit from here is a JSON object on stdout, and this one
        # was a traceback on stderr. The model is told to read the JSON, so a
        # local git that fails -- an unmerged path, a branch that is not there --
        # arrived as something it had no rule for.
        detail = (exc.stderr or "").strip() or f"git exited {exc.returncode}"
        print(
            json.dumps(
                {"error": f"the local git command failed: {detail}"}, indent=2
            )
        )
        return 1
    print(json.dumps(answer, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
