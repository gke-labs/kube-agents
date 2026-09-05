#!/usr/bin/env python3
"""Version control from a container that holds no credential.

Everything that needs the token happens somewhere else. This script talks to
exactly two things: the sandbox's own git, against a local working copy with no
remote, and `POST /v1/vcs/*` on the credential broker over loopback. There is no
third case. No verb here shells out to a network client, none of them names
GitHub, and nothing in this container can reach a forge.

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

    vcs.py clone https://github.com/dshnayder-org/infra
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
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

import credential_proxy_client  # noqa: E402

# The sandbox's own git, off PATH on purpose. `git` on PATH is the
# credential-proxy shim and runs in the broker; this one runs here, against a
# working copy with no remote, and is never given a URL.
# deploy/sandbox/Dockerfile says why the two are different binaries.
LOCAL_GIT = os.environ.get("KUBE_AGENTS_LOCAL_GIT", "/opt/vcs/libexec/git")

ROOT = Path(os.environ.get("KUBE_AGENTS_VCS_ROOT", "/opt/data/scratch/vcs"))
SESSIONS = ROOT / ".sessions"

# Who the local revisions are authored by. Overridable, but it needs a value:
# git refuses to commit without one and the resulting error talks about
# `git config --global`, which is a file this container deliberately has none of.
AUTHOR_NAME = os.environ.get("KUBE_AGENTS_VCS_AUTHOR_NAME", "kube-agents")
AUTHOR_EMAIL = os.environ.get(
    "KUBE_AGENTS_VCS_AUTHOR_EMAIL", "kube-agents@users.noreply.invalid"
)

# A change is manifests, not a build output. The broker enforces its own
# ceilings and would refuse a larger payload anyway; refusing here means the
# caller is told before anything is sent.
MAX_BUNDLE_BYTES = 64 << 20


class VcsError(RuntimeError):
    pass


# ---- the broker -----------------------------------------------------------


def call(verb: str, payload: dict) -> dict:
    endpoint = os.environ.get("CREDENTIAL_PROXY_URL", "").strip()
    if not endpoint:
        raise VcsError(
            "CREDENTIAL_PROXY_URL is not set, so there is no broker to ask. "
            "This skill runs in the shell sandbox."
        )
    try:
        return credential_proxy_client.vcs_call(endpoint, verb, payload)
    except credential_proxy_client.WorkspaceUnavailable as exc:
        # There is no switch for this, so reaching it means the broker in this
        # install predates the routes. Said plainly, because the alternative
        # reading -- that something here can be turned on -- sends whoever hits
        # it looking for a configuration field that does not exist.
        raise VcsError(
            f"this broker does not serve the version-control routes: {exc}. "
            "Its image is older than this skill."
        ) from exc
    except credential_proxy_client.WorkspaceRequestError as exc:
        raise VcsError(exc.payload.get("error", str(exc))) from exc


# ---- the local working copy ----------------------------------------------


def local_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """git, in this container, on a repository with no remote.

    The environment is the argument that this cannot execute anything the
    repository supplied. A bundle carries objects and refs and no config, so the
    only config this copy has is the one git just wrote — which means a
    `.gitattributes` naming `filter.foo.clean` finds no `foo` defined and is
    inert, the same reasoning `content_workspace` makes about the broker's
    trees. `core.hooksPath` is pointed at an empty directory rather than left to
    default, because a hook is the one thing that would not need a config entry
    to have been supplied.
    """
    empty = ROOT / ".no-hooks"
    empty.mkdir(parents=True, exist_ok=True)
    (ROOT / ".home").mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "HOME": str(ROOT / ".home"),
        }
    )
    if not Path(LOCAL_GIT).exists():
        raise VcsError(
            f"{LOCAL_GIT} is not present. This skill needs the sandbox's local "
            "git; on an image without it, use the inspect-repository skill."
        )
    argv = [
        LOCAL_GIT,
        "-c", f"core.hooksPath={empty}",
        "-c", "protocol.ext.allow=never",
        "-c", "protocol.file.allow=always",
        "-c", f"user.name={AUTHOR_NAME}",
        "-c", f"user.email={AUTHOR_EMAIL}",
        *args,
    ]
    return subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        timeout=600,
        env=environment,
    )


def _slug(forge: str, repo: str) -> str:
    return f"{forge}__{repo.replace('/', '__')}"


def session_path(forge: str, repo: str) -> Path:
    return SESSIONS / f"{_slug(forge, repo)}.json"


def save_session(data: dict) -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    session_path(data["forge"], data["repo"]).write_text(json.dumps(data, indent=2))


def all_sessions() -> list[dict]:
    if not SESSIONS.is_dir():
        return []
    found = []
    for path in sorted(SESSIONS.glob("*.json")):
        try:
            found.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            continue
    return found


def _matches(session: dict, spec: str) -> bool:
    """Whether this working copy is the one `spec` names.

    Matched against what the caller typed and against what the broker resolved
    it to, so `infra`, `dshnayder-org/infra` and the full URL all find the same
    copy. Deliberately not re-derived here: parsing a URL into a forge and a
    repository is the broker's job, and a second parser in this container is a
    second thing to keep in agreement.
    """
    wanted = spec.strip().lower().rstrip("/").removesuffix(".git")
    candidates = {
        (session.get("repo") or "").lower(),
        (session.get("spec") or "").lower().rstrip("/").removesuffix(".git"),
    }
    if wanted in candidates:
        return True
    repo = (session.get("repo") or "").lower()
    return bool(repo) and (wanted.endswith("/" + repo) or repo.endswith("/" + wanted))


def resolve_session(spec: str | None) -> dict:
    """Which working copy a verb is about.

    Named, then inferred from the directory the caller is standing in, then the
    only one there is. That is the order every version-control system resolves
    it in, and the last case is what makes `vcs.py log` work right after a clone
    without repeating the URL.
    """
    sessions = all_sessions()
    if spec:
        hits = [session for session in sessions if _matches(session, spec)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise VcsError(
                f"no local copy of {spec}. Run `vcs.py clone {spec}` first."
            )
        raise VcsError(
            f"{spec} matches more than one local copy: "
            + ", ".join(sorted(hit["repo"] for hit in hits))
        )
    if not sessions:
        raise VcsError(
            "there is no local copy of anything yet. Run `vcs.py clone <url>`."
        )
    here = Path.cwd().resolve()
    for session in sessions:
        path = Path(session["path"]).resolve()
        if here == path or path in here.parents:
            return session
    if len(sessions) == 1:
        return sessions[0]
    raise VcsError(
        "several repositories are cloned here; say which with --repo: "
        + ", ".join(sorted(session["repo"] for session in sessions))
    )


def tree_of(session: dict) -> Path:
    tree = Path(session["path"])
    if not tree.is_dir():
        raise VcsError(f"{tree} is gone; clone {session['repo']} again")
    return tree


def _local(session: dict, args: list[str], verb: str) -> dict:
    done = local_git(tree_of(session), *args, check=False)
    return {
        "repo": session["repo"],
        "forge": session["forge"],
        "verb": verb,
        "branch": current_branch(session),
        "exitCode": done.returncode,
        "stdout": done.stdout,
        "stderr": done.stderr.strip()[:2000],
    }


def current_branch(session: dict) -> str:
    done = local_git(
        tree_of(session), "rev-parse", "--abbrev-ref", "HEAD", check=False
    )
    return (done.stdout or "").strip() or session.get("branch", "")


def base_for(session: dict, branch: str) -> str:
    """What a publish of `branch` builds on: its own last published tip.

    Per branch, and that is the whole point. One copy can carry several branches
    -- the second one made after the first was published is the ordinary case --
    and each has a different answer. A single scalar advanced on every publish
    gives the second branch the first branch's tip, which is on no target and
    which the remote has under a name the publish never fetches, so the ancestry
    check refuses and the message blames a rewritten target.

    Falling back to the clone point is what makes a branch's first publish work:
    nothing of it is on the forge yet, so the last thing this copy and the broker
    agreed on is where the copy came from.
    """
    return session.get("published", {}).get(branch) or session["baseRevision"]


# ---- repository verbs -----------------------------------------------------


def verb_capabilities(arguments) -> dict:
    spec = arguments.repository or (arguments.repo if arguments.repo else None)
    if not spec:
        spec = resolve_session(None)["spec"]
    answer = call("capabilities", {"repository": spec})
    answer["localGit"] = LOCAL_GIT if Path(LOCAL_GIT).exists() else None
    return answer


def _refuse_to_discard(destination: Path, *, force: bool) -> None:
    """Stop a re-clone from deleting work that was never published.

    A second `clone` of the same repository replaces the tree, and until this
    check it did so silently -- so a commit made here and not yet published was
    gone with no message. That is the wrong default anywhere; it is worse here
    because `publish` used to answer a moved target by saying to clone again,
    which pointed the caller straight at it.

    Anything at all is enough to refuse: a commit past the recorded base, or an
    uncommitted change, or a git that cannot answer either question. Refusing on
    the third is deliberate -- a tree this cannot read is exactly the one whose
    contents cannot be vouched for.
    """
    if force:
        return
    session = next(
        (s for s in all_sessions() if Path(s.get("path", "")) == destination), None
    )
    reasons = []
    dirty = local_git(destination, "status", "--porcelain", check=False)
    if dirty.returncode != 0:
        reasons.append("its state could not be read")
    elif dirty.stdout.strip():
        reasons.append(f"{len(dirty.stdout.strip().splitlines())} uncommitted change(s)")
    # The base for the branch that is checked out, not the clone point: a branch
    # whose work has been published is not work this would lose, and asking the
    # clone point would count those revisions again and refuse to replace a copy
    # with nothing left in it.
    head = local_git(destination, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    base = (
        base_for(session, (head.stdout or "").strip())
        if session and session.get("baseRevision")
        else None
    )
    if base:
        ahead = local_git(
            destination, "rev-list", "--count", f"{base}..HEAD", check=False
        )
        if ahead.returncode != 0:
            reasons.append("its revisions could not be counted")
        elif (ahead.stdout or "0").strip() not in ("", "0"):
            reasons.append(f"{ahead.stdout.strip()} unpublished revision(s)")
    if not reasons:
        return
    raise VcsError(
        f"there is already a copy at {destination} with "
        + " and ".join(reasons)
        + ". Publish it, or re-run with --force to replace it."
    )


def verb_clone(arguments) -> dict:
    """Bring the repository down as history, not as a directory listing.

    One call to the broker, which clones, bundles and deletes its tree before
    answering. Nothing stays on the credential side after a read, and there is
    no handle to release.
    """
    payload: dict = {"repository": arguments.repository}
    if arguments.branch:
        payload["branch"] = arguments.branch
    answer = call("clone", payload)

    destination = ROOT / _slug(answer["forge"], answer["repo"])
    if destination.exists():
        _refuse_to_discard(destination, force=arguments.force)
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=str(destination.parent), suffix=".bundle")
    bundle_file = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(base64.b64decode(answer["bundleBase64"]))
        # Cloning from a file gives the copy an `origin` pointing at the bundle.
        # It is removed immediately: a remote is a thing a later command can be
        # talked into fetching from or pushing to, and there is nothing here
        # that should ever do either. Revisions go up through `publish`.
        local_git(
            destination.parent,
            "clone", "--quiet", "--no-recurse-submodules",
            "--branch", answer["branch"], str(bundle_file), str(destination),
        )
        local_git(destination, "remote", "remove", "origin", check=False)
    finally:
        bundle_file.unlink(missing_ok=True)

    session = {
        "forge": answer["forge"],
        "repo": answer["repo"],
        "spec": arguments.repository,
        "branch": answer["branch"],
        # What `publish` proves its revisions descend from. Recorded at clone
        # time and never updated by a local commit: it is the last point the
        # broker and this container agreed on.
        "baseRevision": answer["revision"],
        "path": str(destination),
    }
    save_session(session)
    tracked = local_git(destination, "ls-files").stdout.splitlines()
    return {
        "forge": answer["forge"],
        "repo": answer["repo"],
        "branch": answer["branch"],
        "revision": answer["revision"],
        "path": str(destination),
        "files": len(tracked),
        "bundleBytes": answer["size"],
        # Said out loud because it is the whole reason this verb exists: the
        # thing on disk is a repository, and every question about its past is
        # answerable here without asking anybody for a credential. History is
        # always complete — a bundle cannot carry a shallow boundary, so there
        # is no truncated case for a caller to have to notice.
        "history": "complete",
        "remotes": [],
    }


def verb_log(arguments) -> dict:
    session = resolve_session(arguments.repo)
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
    return _local(session, args, "log")


def verb_show(arguments) -> dict:
    session = resolve_session(arguments.repo)
    return _local(session, ["show", arguments.revision], "show")


def verb_diff(arguments) -> dict:
    session = resolve_session(arguments.repo)
    args = ["diff"]
    if arguments.revision:
        args.append(arguments.revision)
    if arguments.paths:
        args += ["--", *arguments.paths]
    return _local(session, args, "diff")


def verb_annotate(arguments) -> dict:
    session = resolve_session(arguments.repo)
    args = ["annotate", "--date=short"]
    if arguments.revision:
        args.append(arguments.revision)
    args += ["--", arguments.path]
    return _local(session, args, "annotate")


def verb_files(arguments) -> dict:
    """The manifest: every tracked path with the mode the revision records.

    The mode is the point. Whether a script is executable is a property of the
    tree entry, and it is the one thing a protocol that carries only bytes has
    nowhere to put — which is why this verb exists rather than `ls`.
    """
    session = resolve_session(arguments.repo)
    args = ["ls-files", "--stage"]
    if arguments.paths:
        args += ["--", *arguments.paths]
    result = _local(session, args, "files")
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
    session = resolve_session(arguments.repo)
    args = ["grep", "--line-number"]
    if arguments.ignore_case:
        args.append("--ignore-case")
    args.append("--extended-regexp" if arguments.regex else "--fixed-strings")
    args += ["-e", arguments.pattern]
    if arguments.paths:
        args += ["--", *arguments.paths]
    result = _local(session, args, "grep")
    # git grep exits 1 for "no match", which is an answer rather than a failure
    # and should not read to the caller as one.
    if result["exitCode"] == 1 and not result["stderr"]:
        result["exitCode"] = 0
        result["matches"] = 0
    else:
        result["matches"] = len(result["stdout"].splitlines())
    return result


def verb_status(arguments) -> dict:
    session = resolve_session(arguments.repo)
    result = _local(session, ["status", "--porcelain=v1"], "status")
    result["changes"] = [
        {"state": line[:2].strip(), "path": line[3:]}
        for line in result["stdout"].splitlines()
        if line
    ]
    result["count"] = len(result["changes"])
    del result["stdout"]
    return result


def verb_branch(arguments) -> dict:
    """List the lines of development, or start one.

    Local only, and it makes no network call. A branch is a name for a revision;
    it becomes something the forge knows about when `publish` sends the
    revisions under it, not before.
    """
    session = resolve_session(arguments.repo)
    tree = tree_of(session)
    if not arguments.name:
        listing = local_git(tree, "branch", "--format=%(refname:short)", check=False)
        return {
            "repo": session["repo"],
            "forge": session["forge"],
            "verb": "branch",
            "branch": current_branch(session),
            "branches": listing.stdout.split(),
            "exitCode": listing.returncode,
            "stderr": listing.stderr.strip()[:2000],
        }
    exists = local_git(
        tree, "rev-parse", "--verify", "--quiet", f"refs/heads/{arguments.name}",
        check=False,
    )
    switch = ["switch", arguments.name] if exists.returncode == 0 else [
        "switch", "--create", arguments.name
    ]
    done = local_git(tree, *switch, check=False)
    return {
        "repo": session["repo"],
        "forge": session["forge"],
        "verb": "branch",
        "branch": current_branch(session),
        "created": exists.returncode != 0,
        "exitCode": done.returncode,
        "stderr": done.stderr.strip()[:2000],
    }


def verb_commit(arguments) -> dict:
    """Record a revision, here, with the sandbox's own git.

    Local on purpose. The revision has a real parent and a real identifier
    before anything leaves this container, so `log` shows the work in progress,
    a branch of five changes stays five revisions rather than being flattened
    into one, and `publish` has something whose ancestry it can prove.
    """
    session = resolve_session(arguments.repo)
    tree = tree_of(session)
    staged = local_git(
        tree, "add", "--", *arguments.paths, check=False
    ) if arguments.paths else local_git(tree, "add", "--all", check=False)
    if staged.returncode != 0:
        raise VcsError(f"nothing was staged: {staged.stderr.strip()}")
    pending = local_git(tree, "diff", "--cached", "--name-only", check=False)
    changed = [line for line in pending.stdout.splitlines() if line]
    if not changed:
        raise VcsError(
            "there is nothing to record. `vcs.py status` shows what the working "
            "copy has that its revision does not."
        )
    done = local_git(tree, "commit", "--message", arguments.message, check=False)
    if done.returncode != 0:
        raise VcsError(f"commit failed: {(done.stderr or done.stdout).strip()}")
    revision = local_git(tree, "rev-parse", "HEAD").stdout.strip()
    return {
        "repo": session["repo"],
        "forge": session["forge"],
        "verb": "commit",
        "branch": current_branch(session),
        "revision": revision,
        "files": changed,
        "count": len(changed),
        "published": False,
    }


def verb_publish(arguments) -> dict:
    """Send the revisions made since `clone` to the shared repository.

    Symmetric with `clone`: history goes up the way it came down, as a bundle of
    objects and refs. The broker fetches the base, unpacks the bundle beside it,
    checks that the tip descends from the revision it handed out, and pushes the
    branch — without ever checking the objects out. So the revision identifiers
    on the forge are the ones `log` printed here.
    """
    session = resolve_session(arguments.repo)
    tree = tree_of(session)
    branch = current_branch(session)
    base = base_for(session, branch)
    target = arguments.target or session["branch"]
    ahead = local_git(tree, "rev-list", "--count", f"{base}..HEAD", check=False)
    if ahead.returncode != 0:
        raise VcsError(
            f"cannot compare against {base[:12]}: {ahead.stderr.strip()}"
        )
    count = int((ahead.stdout or "0").strip() or "0")
    if count == 0:
        raise VcsError(
            "there are no new revisions to publish. `vcs.py commit` records "
            "one; `vcs.py status` shows what is still uncommitted."
        )
    if branch == target:
        # After the count, not before: on the shared branch with nothing
        # committed, "there is nothing to publish" is the more specific of the
        # two true things and the one that says what to do next.
        #
        # Refused here as well as in the broker, and the broker's is the
        # control -- this only saves the round trip and the bundle. Worth having
        # because it is the mistake with no signal: `clone`, `commit`,
        # `publish` with no `--target` reads like the obvious sequence right up
        # to the 409.
        raise VcsError(
            f"you are on {branch}, which is the branch this copy was cloned "
            "from, so this would write to the shared branch. Make a branch of "
            "your own with `vcs.py branch <name>` and publish that."
        )

    handle, name = tempfile.mkstemp(dir=str(ROOT), suffix=".bundle")
    bundle_file = Path(name)
    os.close(handle)
    try:
        made = local_git(
            tree, "bundle", "create", str(bundle_file), branch, f"^{base}",
            check=False,
        )
        if made.returncode != 0:
            raise VcsError(f"could not bundle the revisions: {made.stderr.strip()}")
        blob = bundle_file.read_bytes()
        if len(blob) > MAX_BUNDLE_BYTES:
            raise VcsError(
                f"the change is {len(blob)} bytes, over the "
                f"{MAX_BUNDLE_BYTES}-byte ceiling for one publish"
            )
        answer = call(
            "publish",
            {
                "repository": session["spec"],
                "branch": branch,
                "target": target,
                "baseRevision": base,
                "bundleBase64": base64.b64encode(blob).decode("ascii"),
            },
        )
    finally:
        bundle_file.unlink(missing_ok=True)

    # The published tip becomes this branch's base. A second publish of the same
    # branch then sends only what came after it, and its ancestry check is
    # against something the remote demonstrably has.
    session.setdefault("published", {})[answer["branch"]] = answer["revision"]
    session["publishedBranch"] = answer["branch"]
    save_session(session)
    answer["revisions"] = count
    return answer


def verb_discard(arguments) -> dict:
    """Remove the local copy. Nothing is released on the credential side.

    There is nothing there to release — every broker route is one request long.
    This deletes a directory, and it is called `discard` rather than `close` for
    that reason: closing implies a counterpart that was opened.
    """
    session = resolve_session(arguments.repo)
    shutil.rmtree(session["path"], ignore_errors=True)
    session_path(session["forge"], session["repo"]).unlink(missing_ok=True)
    return {
        "repo": session["repo"],
        "forge": session["forge"],
        "verb": "discard",
        "removed": session["path"],
    }


# ---- collaboration verbs --------------------------------------------------


def _collaboration(arguments, verb: str, payload: dict) -> dict:
    """Every forge call: name the repository, POST, print what comes back.

    The repository is the only thing resolved locally, and only so a caller
    standing in a working copy need not repeat it. Everything else — which forge
    this is, what it calls a change proposal, how to reach its API — is decided
    on the credential side and arrives already translated.
    """
    if arguments.repo:
        payload["repository"] = arguments.repo
    else:
        payload["repository"] = resolve_session(None)["spec"]
    return call(verb, {key: value for key, value in payload.items() if value is not None})


def verb_proposal_create(arguments) -> dict:
    source, target = arguments.source, arguments.target
    if not source or not target:
        session = resolve_session(arguments.repo)
        source = source or current_branch(session)
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
        {"state": arguments.state, "limit": arguments.limit},
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
    repo_option(publish).set_defaults(run=verb_publish)

    discard = verbs.add_parser(
        "discard", aliases=["close"], help="remove the local copy"
    )
    repo_option(discard).set_defaults(run=verb_discard)

    proposal = verbs.add_parser(
        "proposal", aliases=["pr", "mr"], help="change proposals on the forge"
    )
    actions = proposal.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create")
    create.add_argument("--title", required=True)
    create.add_argument("--body", default="")
    create.add_argument("--source", help="the branch to merge (default: current)")
    create.add_argument("--target", help="the branch to merge into (default: cloned)")
    create.add_argument("--draft", action="store_true")
    repo_option(create).set_defaults(run=verb_proposal_create)

    plist = actions.add_parser("list")
    plist.add_argument("--state", default="open", choices=["open", "closed", "all"])
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

    issue = verbs.add_parser("issue", help="work items on the forge")
    iactions = issue.add_subparsers(dest="action", required=True)

    ilist = iactions.add_parser("list")
    ilist.add_argument("--state", default="open", choices=["open", "closed", "all"])
    ilist.add_argument("--labels", nargs="*")
    ilist.add_argument("-n", "--limit", type=int)
    repo_option(ilist).set_defaults(run=verb_issue_list)

    iview = iactions.add_parser("view")
    iview.add_argument("number", type=int)
    iview.add_argument("--comments", action="store_true")
    iview.add_argument("-n", "--limit", type=int)
    repo_option(iview).set_defaults(run=verb_issue_view)

    icreate = iactions.add_parser("create")
    icreate.add_argument("--title", required=True)
    icreate.add_argument("--body", default="")
    icreate.add_argument("--labels", nargs="*")
    repo_option(icreate).set_defaults(run=verb_issue_create)

    icomment = iactions.add_parser("comment")
    icomment.add_argument("number", type=int)
    icomment.add_argument("--body", required=True)
    repo_option(icomment).set_defaults(run=verb_issue_comment)

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        answer = arguments.run(arguments)
    except VcsError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
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
