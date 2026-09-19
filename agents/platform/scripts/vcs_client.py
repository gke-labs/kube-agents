#!/usr/bin/env python3
"""vcs_client.py — the version-control verbs, as a library the sandbox's Python can import.

`vcs.py` is the command line the model drives; this is the same thing for the
scripts that used to shell `gh` and the credential shim -- the issue resolver,
the suggestion submitter, the audit ledger -- so that a consumer reaches a forge
the way the skill does: one broker call per verb, a working copy with no remote
and no credential for everything local. Extracted rather than reimplemented,
so the two callers cannot drift.

Everything that spends a credential goes through `call()`, which is
`POST /v1/vcs/<verb>` on the credential broker. Everything else runs the
sandbox's own git, by absolute path, against a copy `clone()` unpacked from a
bundle. The session store under `ROOT/.sessions` remembers, per working copy,
which branch the copy was cloned from and what each branch last published --
the two facts `publish()` proves its revisions against.

The operations mirror the command line: `capabilities`, `clone`, `branch`,
`commit`, `publish`, `discard`, `forge(verb, payload)` for the collaboration
verbs, and `local(session, args, verb)` for a read. Each returns the JSON the
command line would have printed and raises `VcsError` where it would have
printed one.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path

# `/opt/defaults/scripts` and `/opt/data/scripts` are how a script in the agent
# pod finds its siblings. They are left off when this file *is* the trusted copy
# the sandbox runs as `hermes`: the entrypoint chowns both to `agent`, and a
# root-owned process must not carry a directory uid 1000 can write on its import
# path at all, even behind site-packages. Nothing is lost by dropping them there
# -- `sys.path[0]` is the trusted directory itself, the closure staged in it is
# complete, and deploy/sandbox/trusted-closure-guard.py fails the build if it
# ever stops being.
TRUSTED_CLOSURE = "/opt/vcs/libexec/platform"
if not str(Path(__file__).resolve()).startswith(TRUSTED_CLOSURE + "/"):
    sys.path.append("/opt/defaults/scripts")
    sys.path.append("/opt/data/scripts")
sys.path.append(str(Path(__file__).resolve().parent))

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
    """A refusal the caller is meant to read, as JSON on stdout.

    `code` and `detail` are the broker's, when the refusal was the broker's:
    SKILL.md's rules are written against the codes -- `BASE_MOVED` means clone
    again, `FORGE_RATE_LIMITED` means wait, `FORGE_REJECTED` means read the
    detail -- so a client that reduced the answer to its message would be
    handing the agent a decision keyed on a field it never receives.
    """

    def __init__(self, message: str, *, code: str | None = None, detail: str | None = None):
        super().__init__(message)
        self.code = code or None
        self.detail = detail or None

    def as_json(self) -> dict:
        answer = {"error": str(self)}
        if self.code:
            answer["code"] = self.code
        if self.detail:
            answer["detail"] = self.detail
        return answer


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
        payload = exc.payload or {}
        raise VcsError(
            payload.get("error", str(exc)),
            code=payload.get("code"),
            detail=payload.get("detail"),
        ) from exc
    except credential_proxy_client.TokenUnavailable as exc:
        # The projected token is missing or empty -- the kubelet mid-rewrite,
        # or a volume that was never projected. Nothing the caller can do from
        # here except retry; saying so as JSON keeps the contract every other
        # failure keeps.
        raise VcsError(
            f"the broker credential is not readable: {exc}. Retry shortly; "
            "if it persists the sandbox's token volume is not projected."
        ) from exc
    except urllib.error.URLError as exc:
        # Connection refused or dropped: a restarting broker, or a policy in
        # the way. HTTP errors never reach here -- vcs_call turns them into
        # WorkspaceRequestError above -- so this is the transport failing.
        raise VcsError(
            f"the broker at {endpoint} could not be reached: {exc.reason}. "
            "Retry shortly."
        ) from exc
    except ValueError as exc:
        raise VcsError(
            "the broker answered with something that is not JSON; retry, and "
            "if it persists the broker is unhealthy."
        ) from exc


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
            # `file` only, for the git this script runs. A default rather than
            # a control -- the design says why an environment setting cannot
            # be one inside the sandbox; the image's deletions are the control.
            "GIT_ALLOW_PROTOCOL": "file",
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


def _slug(forge: str, repo: str, key: str) -> str:
    """The directory one working copy gets, under a root every card shares.

    Keyed on the branch as well as the repository, because one repository worked
    on by two cards at once is the ordinary case for a fleet: nine audit streams
    and a suggestion card all target the same GitOps repository. Keyed on the
    repository alone, the second `prepare` of the day either refused -- there is
    already a copy here -- or, with `--force`, deleted the first card's
    unpublished work.

    `/` becomes `__` the way it already does in the repository name. A branch
    name cannot contain `_` at all (`providers/validate.BRANCH_RE`), so no two
    branches of one repository can land on the same directory.
    """
    return f"{forge}__{repo.replace('/', '__')}__{key.replace('/', '__')}"


def _key_of(session: dict) -> str:
    """Which branch the copy is *for*, which is not always the one it is *of*.

    A branch this run is starting does not exist on the forge yet, so the copy
    is taken of the base and the branch is cut from it locally: `branch` is the
    base, and `key` is the name the work will be published under. They are the
    same string whenever the branch already existed.
    """
    return session.get("key") or session.get("branch") or ""


def session_path(forge: str, repo: str, key: str) -> Path:
    return SESSIONS / f"{_slug(forge, repo, key)}.json"


def save_session(data: dict) -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    path = session_path(data["forge"], data["repo"], _key_of(data))
    path.write_text(json.dumps(data, indent=2))


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
    it to, so `infra`, `acme/infra` and the full URL all find the same
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


def _listing(sessions: list[dict]) -> str:
    """The copies, as something the caller can act on rather than choose from.

    The path, because that is what `cd` takes and the working copy is where
    every one of these verbs wants the caller to be standing anyway.
    """
    return ", ".join(
        sorted(
            f"{session['path']} ({session['repo']} on {_key_of(session)})"
            for session in sessions
        )
    )


def _standing_in(sessions: list[dict]) -> dict | None:
    """The copy the caller is inside, if it is inside one."""
    here = Path.cwd().resolve()
    for session in sessions:
        path = Path(session["path"]).resolve()
        if here == path or path in here.parents:
            return session
    return None


def resolve_session(spec: str | None = None, key: str | None = None) -> dict:
    """Which working copy a verb is about.

    Named, then inferred from the directory the caller is standing in, then the
    only one there is. That is the order every version-control system resolves
    it in, and the last case is what makes `vcs.py log` work right after a clone
    without repeating the URL.

    `key` is the branch a caller that knows which change it is working on passes
    -- `submit-suggestion` has it from `--branch`. It matters because one
    repository can be cloned twice here, once per card, and then the repository
    alone names two copies. A caller that does not have it is not stuck: the
    directory it is standing in still decides, which is what the agent following
    the SKILL does, and the refusal at the end names the branches to choose
    between rather than repeating the repository twice.
    """
    sessions = all_sessions()
    if key:
        sessions = [session for session in sessions if _key_of(session) == key]
    named = f"{spec} on '{key}'" if spec and key else (spec or f"'{key}'")
    if spec or key:
        hits = [session for session in sessions if not spec or _matches(session, spec)]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise VcsError(
                f"no local copy of {named}. Run `vcs.py clone {spec or '<url>'}`"
                " first."
            )
        # More than one copy of the same repository, and the caller named no
        # branch. Standing inside one of them is an answer.
        standing = _standing_in(hits)
        if standing:
            return standing
        raise VcsError(
            f"{named} is cloned here more than once, one copy per branch. Run "
            "this from inside the one you mean: " + _listing(hits)
        )
    if not sessions:
        raise VcsError(
            "there is no local copy of anything yet. Run `vcs.py clone <url>`."
        )
    standing = _standing_in(sessions)
    if standing:
        return standing
    if len(sessions) == 1:
        return sessions[0]
    raise VcsError(
        "several working copies are here; name one with --repo, or run this "
        "from inside it: " + _listing(sessions)
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




def local(session: dict, args: list[str], verb: str) -> dict:
    """One local git read against the copy, answered as the command line does."""
    return _local(session, args, verb)


def capabilities(repository: str | None = None) -> dict:
    """What this install can do with a repository, before anything is spent."""
    spec = repository or resolve_session(None)["spec"]
    return call("capabilities", {"repository": spec})


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


def clone(
    repository: str,
    branch: str | None = None,
    *,
    force: bool = False,
    key: str | None = None,
) -> dict:
    """Bring the repository down as history, not as a directory listing.

    One call to the broker, which clones, bundles and deletes its tree before
    answering. Nothing stays on the credential side after a read, and there is
    no handle to release.
    """
    payload: dict = {"repository": repository}
    if branch:
        payload["branch"] = branch
    answer = call("clone", payload)

    # `key` is for the caller that is about to cut a branch this copy is not on
    # yet: `submit-suggestion` clones the base and then switches, and the copy
    # belongs to that branch, not to the base every other card also clones.
    key = key or branch or answer["branch"]
    destination = ROOT / _slug(answer["forge"], answer["repo"], key)
    if destination.exists():
        _refuse_to_discard(destination, force=force)
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
        "spec": repository,
        "branch": answer["branch"],
        # What this copy is for, and what its directory is named after.
        "key": key,
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


def branch(
    spec: str | None = None, name: str | None = None, key: str | None = None
) -> dict:
    """List the lines of development, or start one.

    Local only, and it makes no network call. A branch is a name for a revision;
    it becomes something the forge knows about when `publish` sends the
    revisions under it, not before.
    """
    session = resolve_session(spec, key=key)
    tree = tree_of(session)
    if not name:
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
        tree, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}",
        check=False,
    )
    switch = ["switch", name] if exists.returncode == 0 else ["switch", "--create", name]
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


def _untracked(tree: Path) -> list[str]:
    """Paths in the working copy that git does not track, respecting ignores.

    `--others --exclude-standard` is the pair: the first asks for what is not
    tracked, the second keeps `.gitignore` honoured, so a repository that
    already ignores its own build output does not have to be argued with.
    """
    found = local_git(
        tree, "ls-files", "--others", "--exclude-standard", check=False
    )
    return [line for line in found.stdout.splitlines() if line.strip()]


def commit(
    message: str,
    paths: list[str] | tuple[str, ...] = (),
    spec: str | None = None,
    key: str | None = None,
) -> dict:
    """Record a revision, here, with the sandbox's own git.

    Local on purpose. The revision has a real parent and a real identifier
    before anything leaves this container, so `log` shows the work in progress,
    a branch of five changes stays five revisions rather than being flattened
    into one, and `publish` has something whose ancestry it can prove.

    **With no `paths`, this records changes to files the copy already tracks
    and nothing else.** It is not `add --all`. The working copy is a real clone
    on a filesystem the caller also scratches in, and a blanket add is how a
    log, a debug dump or a half-written note ends up in a public proposal --
    `submit-suggestion/SKILL.md` forbids `git add .` for exactly that reason,
    and a helper that does it on the caller's behalf forbids nothing. An
    untracked file is therefore a refusal naming it rather than something
    silently swept in or silently left out: either answer, given without
    saying so, is one the caller would have wanted to know about.
    """
    session = resolve_session(spec, key=key)
    tree = tree_of(session)
    if paths:
        staged = local_git(tree, "add", "--", *paths, check=False)
    else:
        untracked = _untracked(tree)
        if untracked:
            shown = ", ".join(untracked[:10])
            more = f" (and {len(untracked) - 10} more)" if len(untracked) > 10 else ""
            raise VcsError(
                f"{len(untracked)} file(s) here are not tracked yet and will not "
                f"be recorded on their own: {shown}{more}. Name the ones that "
                "belong in the change -- `vcs.py commit --message ... <path>...` "
                "-- and delete the rest. This never stages a file you did not "
                "name, because a working copy is also where scratch output lands."
            )
        # `--update`, not `--all`: tracked files only, deletions included.
        staged = local_git(tree, "add", "--update", check=False)
    if staged.returncode != 0:
        raise VcsError(f"nothing was staged: {staged.stderr.strip()}")
    pending = local_git(tree, "diff", "--cached", "--name-only", check=False)
    changed = [line for line in pending.stdout.splitlines() if line]
    if not changed:
        raise VcsError(
            "there is nothing to record. `vcs.py status` shows what the working "
            "copy has that its revision does not."
        )
    done = local_git(tree, "commit", "--message", message, check=False)
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


def already_published(session: dict, branch: str | None = None) -> bool:
    """Is this branch's current tip already on the remote?

    Read off the session, which records the tip each `publish` landed, against
    the tip the copy holds now. It is not a question about the forge: the
    session is written after the broker answered, so a `True` here means this
    container watched those revisions land.

    It exists for one state, and that state is reachable: `publish` succeeded
    and whatever the caller did next did not. Without this, the retry finds
    nothing new to send and is refused before it reaches the step that failed.
    """
    branch = branch or current_branch(session)
    tip = local_git(tree_of(session), "rev-parse", "HEAD", check=False)
    if tip.returncode != 0:
        return False
    return (session.get("published") or {}).get(branch) == tip.stdout.strip()


def publish(
    spec: str | None = None,
    target: str | None = None,
    *,
    advance: bool = False,
    key: str | None = None,
) -> dict:
    """Send the revisions made since `clone` to the shared repository.

    Symmetric with `clone`: history goes up the way it came down, as a bundle of
    objects and refs. The broker fetches the base, unpacks the bundle beside it,
    checks that the tip descends from the revision it handed out, and pushes the
    branch — without ever checking the objects out. So the revision identifiers
    on the forge are the ones `log` printed here.

    `advance` says this copy was cloned *of* a proposal branch in order to add
    to it, which is the one reason to publish the branch the copy came down on.
    It needs an explicit `target` beside it — the branch the proposal merges
    into — because the default target is the cloned branch itself.
    """
    session = resolve_session(spec, key=key)
    tree = tree_of(session)
    branch = current_branch(session)
    base = base_for(session, branch)
    target = target or session["branch"]
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
    # Both of these come after the count, not before: on the shared branch with
    # nothing committed, "there is nothing to publish" is the more specific of
    # the two true things and the one that says what to do next.
    if branch == target:
        raise VcsError(
            f"branch and target are both {branch}, so this would write the "
            "revisions straight onto the branch they are meant to be proposed "
            "for. Name the branch this work merges into as the target."
        )
    if branch == session["branch"] and not advance:
        # This clause, not the one above, is what caught the real case. `branch
        # == target` alone was defeated by `--target <anything else>` while
        # still standing on the branch the copy was cloned from -- seen live: a
        # worker cloned a non-default branch, committed on it, published with
        # `--target main`, and fast-forwarded the branch it had cloned. The copy
        # knows which branch that was; the broker does not, so the copy is where
        # the refusal is exact. The broker refuses the same thing when told
        # (`clonedFrom` below) and refuses the remote's default branch on its
        # own.
        #
        # `advance` is the one way past it, and it is a different sentence
        # rather than a louder one: the copy was cloned *of* a proposal branch
        # so that this publish could add to it. Nothing else is waived.
        raise VcsError(
            f"you are on {branch}, which is the branch this copy was cloned "
            "from, so this would write to it directly. Make a branch of your "
            "own with `vcs.py branch <name>` and publish that -- or, if you "
            "cloned this branch to add to the proposal already on it, say so "
            "with `--advance`."
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
                "clonedFrom": session["branch"],
                "advance": advance,
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


def discard(spec: str | None = None, key: str | None = None) -> dict:
    """Remove the local copy. Nothing is released on the credential side.

    There is nothing there to release — every broker route is one request long.
    This deletes a directory, and it is called `discard` rather than `close` for
    that reason: closing implies a counterpart that was opened.
    """
    session = resolve_session(spec, key=key)
    shutil.rmtree(session["path"], ignore_errors=True)
    session_path(
        session["forge"], session["repo"], _key_of(session)
    ).unlink(missing_ok=True)
    return {
        "repo": session["repo"],
        "forge": session["forge"],
        "verb": "discard",
        "removed": session["path"],
    }


# ---- collaboration verbs --------------------------------------------------


def forge(verb: str, payload: dict, repository: str | None = None) -> dict:
    """Every forge call: name the repository, POST, print what comes back.

    The repository is the only thing resolved locally, and only so a caller
    standing in a working copy need not repeat it. Everything else — which forge
    this is, what it calls a change proposal, how to reach its API — is decided
    on the credential side and arrives already translated.
    """
    if repository:
        payload["repository"] = repository
    else:
        payload["repository"] = resolve_session(None)["spec"]
    return call(verb, {key: value for key, value in payload.items() if value is not None})
