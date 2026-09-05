#!/usr/bin/env python3
"""Version control as concepts: the `/v1/vcs/*` broker routes.

The credential is here and the working copy is not. A caller in the sandbox
names a repository by URL and asks for the things version control is for; every
one of them is answered by this process, which holds the token, on behalf of a
process that does not.

Three properties are the whole design.

*The forge is decided here.* Which forge a URL belongs to and which credential
opens it are the same question, and the answer belongs beside the credential. A
sandbox that had to tell one forge from another would be a second place that
has to agree, and adding a forge would mean shipping two images instead of one.
So the caller sends a URL, the registry maps its host through a configured
table, and a host with no entry is refused by name rather than attempted with
whatever credential happens to be loaded. That table is also the security
boundary: a caller-chosen URL decides where a token gets sent, and an allowlist
is what stops "clone this repository" from meaning "post my credential there".

*Nothing crosses this seam in a forge's own vocabulary.* The forge's API is
called from this process because this is where its credential lives, and its
JSON stops here. What goes back is a normalised proposal, issue or comment --
the concepts every forge has under a different name. A caller that received one
forge's field names would be that forge's client wearing a neutral URL, and the
second forge would be a second client rather than a second directory.

*History moves as bundles, in both directions, and is never checked out here.*
`clone` clones, bundles, and deletes the tree before it answers. `publish` takes
a bundle of the caller's new revisions, fetches it into a scratch repository,
checks that it says what it claims to say, and pushes the branch -- without ever
running a `checkout`. That last part is what makes accepting caller-supplied
objects safe: a `.gitattributes` naming a filter driver, a `.gitmodules`, a file
called `.gitconfig` are all inert as long as nothing materialises them into a
working copy beside the token. Objects and refs are data; a checkout is what
turns them into behaviour.

The routes are stateless. There is no handle, nothing survives a request, and
two concurrent requests share nothing but the lock the HTTP layer holds.

This file names no forge, and a test enforces that. Everything a forge decides
is behind `providers`; everything here is true whatever forge the URL named.

On the vocabulary
-----------------
The verb names are the version-control concepts rather than one system's
spelling of them, because the caller is a language model and the concepts are
what it was trained on. Where the systems disagree the neutral name wins and the
familiar one is an alias: `annotate` over `blame`, which is what Mercurial,
Subversion, Bazaar and jj all call per-line attribution and which git itself
accepts; `publish` over `push`, because sending revisions to the shared
repository is the concept and `push` is the DVCS spelling that invites `--force`
and an `origin` this design does not have. On the collaboration side the neutral
noun is `proposal`, after Launchpad's "merge proposal" -- the term `breezy` and
`silver-platter` settled on for exactly this problem -- with `pr` and `mr` as
aliases, since "pull request" carries a fork-and-branch assumption not every
forge shares and Gerrit's unit of review is a single revision.
`docs/designs/version-control-support.md` records the sources.
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from providers import (
    CliTransport,
    Forge,
    ForgeUnsupported,
    Registry,
    Transport,
    validate_branch,
    validate_revision,
)
from workspace_paths import WorkspaceError

LOGGER = logging.getLogger("credential-proxy.vcs")

# The same shape of ceiling `content_workspace` applies, for the same reason:
# the broker's scratch volume is an emptyDir sized for manifests, and a
# repository that does not fit should say so rather than fill the disk out from
# under everything else.
DEFAULT_MAX_CLONE_BYTES = 256 << 20  # 256 MiB
DEFAULT_MAX_BUNDLE_BYTES = 64 << 20  # 64 MiB

# The ref an incoming bundle is fetched into. Under `refs/vcs/` rather than
# `refs/heads/` so nothing here can be confused with a branch, and so a publish
# of a leftover ref cannot happen by naming a plausible branch.
_INCOMING = "refs/vcs/incoming"


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        LOGGER.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value <= 0:
        LOGGER.warning("%s=%r is not positive; using %d", name, raw, default)
        return default
    return value


def max_bundle_bytes() -> int:
    """The publish ceiling, readable before a broker exists.

    The HTTP layer in front of these routes has its own body limit, and it has
    to be sized from this number or the smaller of the two is what actually
    refuses -- at a size no error code names and no document advertises. It
    reads the ceiling here rather than restating it.
    """
    return _positive_int("CREDENTIAL_PROXY_MAX_BUNDLE_BYTES", DEFAULT_MAX_BUNDLE_BYTES)


def _remove_tree(path: Path) -> None:
    for entry in sorted(path.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if entry.is_dir() and not entry.is_symlink():
                entry.rmdir()
            else:
                entry.unlink()
        except OSError:
            pass
    try:
        path.rmdir()
    except OSError:
        pass


class Binding:
    """One request's forge, repository, transport and git runner.

    Resolution answers two questions at once -- which forge, and which
    repository of it -- and everything after that needs a third and a fourth:
    how to reach its API, and what git needs on its behalf. Bundling them means
    the verbs below read as version control rather than as wiring, and it is
    the only place that touches a credential at all. The broker does not know
    whether making one current means minting a token or doing nothing.
    """

    def __init__(
        self,
        forge: Forge,
        repo: str,
        transport: Callable[[], Transport],
        git: Callable,
    ):
        self.forge = forge
        self.repo = repo
        self.git = git
        # Built on the first call rather than up front. A verb this forge does
        # not serve refuses before anything is constructed for it, so the
        # answer names the missing verb rather than the transport that was
        # never going to be used.
        self._transport = transport
        self._built: Transport | None = None
        self._ready = False

    def ensure(self) -> None:
        """Make the credential current, once per request, before it is spent.

        Before rather than after a failure. A token that expired while the pod
        was idle surfaces from inside the broker's own clone as
        `Authentication failed`, which reaches the caller as a clone failure and
        reads like the repository is gone.
        """
        if not self._ready:
            self.forge.credential.ensure(self.repo)
            self._ready = True

    def api(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._built is None:
            self._built = self._transport()
        self.ensure()
        return self._built.api(method, path, **kwargs)

    def stamp(self, result: dict[str, Any]) -> dict[str, Any]:
        result.update({"forge": self.forge.name, "repo": self.repo})
        return result


class VcsBroker:
    """The verbs, each one request long.

    `scratch_root` is on the broker's own volume. Nothing under it outlives a
    request, which is what makes these routes stateless: there is no handle to
    leak, no tree to collide with another caller's, and no cleanup an
    interrupted client can skip.
    """

    def __init__(
        self,
        scratch_root: str | Path,
        git_runner: Callable[..., subprocess.CompletedProcess],
        cli_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        refresh: Callable[[str, str], None] | None = None,
    ) -> None:
        self.scratch_root = Path(scratch_root)
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self._git_runner = git_runner
        # A CLI transport needs the broker's credential environment but no
        # repository. When the caller does not separate the two, the git runner
        # serves both.
        # No timeout here on purpose: the runners are given one by whoever
        # built them, and a second number this class merely stored would read
        # like a bound it enforces.
        self._cli_runner = cli_runner or git_runner
        self.max_clone_bytes = _positive_int(
            "CREDENTIAL_PROXY_MAX_CLONE_BYTES", DEFAULT_MAX_CLONE_BYTES
        )
        self.max_bundle_bytes = max_bundle_bytes()
        # The refresh operation is configuration in the sense that matters: it
        # is how this install performs a privileged act, and a forge decides
        # whether its credential strategy has any use for one.
        self.registry = Registry({"refresh": refresh})
        # Only the counter. Two requests share nothing else -- each gets its own
        # scratch directory and deletes it -- so serialising whole requests
        # would make a clone of one repository wait on a publish of another for
        # no property gained.
        self._sequence = 0
        self._sequence_lock = threading.Lock()

    # ---- plumbing ------------------------------------------------------

    def _bind(self, payload: dict[str, Any]) -> Binding:
        forge, repo = self.registry.resolve(payload.get("repository"))
        return Binding(
            forge,
            repo,
            lambda: self._transport(forge),
            self._git_for(forge, repo),
        )

    def _transport(self, forge: Forge) -> Transport:
        """The transport the forge declared, constructed here and never there.

        A forge names what it needs; the broker owns everything about how the
        call is made -- the executable, the timeout, the output ceiling. Only
        the CLI transport exists so far, because it is the only one a forge in
        this install declares; the seam is what lets the next one be an
        in-process HTTP client rather than a second subprocess.
        """
        if forge.transport == "cli" and forge.cli:
            return CliTransport(self._cli_runner, forge.cli, forge.error_overrides)
        raise ForgeUnsupported(
            f"{forge.name} declares the {forge.transport!r} transport, which "
            "this broker does not build."
        )

    def _git_for(self, forge: Forge, repo: str) -> Callable[..., Any]:
        """A git runner carrying whatever config this forge needs on it.

        Per-invocation, not global. The broker already forces a config layer
        onto every git it runs; this adds to that layer for one forge's own
        invocations, so a credential belonging to one forge is not installed on
        every git in the process.
        """
        config = tuple(forge.credential.git_config(repo))

        def run(cwd: Path, *args: str, check: bool = True):
            return self._git_runner(["git", *args], cwd, check, config)

        return run

    def _scratch(self, kind: str) -> Path:
        """A fresh directory under the broker's root.

        Named from a counter rather than from anything the caller sent. A
        directory named after a repository is a directory two requests for the
        same repository collide in, and the name is also a place a caller-chosen
        string would reach the filesystem.
        """
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        path = self.scratch_root / f"{kind}-{os.getpid()}-{sequence}"
        if path.exists():
            _remove_tree(path)
        path.mkdir(parents=True)
        return path

    def _enforce_ceiling(self, root: Path, repo: str) -> None:
        total = 0
        for directory, _subdirs, filenames in os.walk(root):
            for filename in filenames:
                try:
                    total += os.lstat(os.path.join(directory, filename)).st_size
                except OSError:
                    continue
                if total > self.max_clone_bytes:
                    raise WorkspaceError(
                        f"{repo} is larger than the {self.max_clone_bytes}-byte "
                        "ceiling for a broker-side clone. Name a `branch` to "
                        "fetch one line of development.",
                        status=413,
                        code="CLONE_TOO_LARGE",
                    )

    @staticmethod
    def _default_branch(git: Callable, root: Path) -> str:
        result = git(
            root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD",
            check=False,
        )
        ref = (result.stdout or "").strip()
        if result.returncode == 0 and ref:
            return ref.split("/", 1)[1] if ref.startswith("origin/") else ref
        local = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        return (local.stdout or "").strip() or "main"

    # ---- repository verbs ----------------------------------------------

    def capabilities(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What this install can do with this repository, before anything is spent.

        Answered without making a credential current or touching the network. A
        caller that discovers the gap by failing halfway through a publish has
        already written the revision it cannot deliver.
        """
        try:
            forge, repo = self.registry.resolve(payload.get("repository"))
        except ForgeUnsupported as exc:
            return {
                "forge": None,
                "repo": None,
                "proposalNoun": None,
                "verbs": [],
                "missing": [str(exc)],
            }
        return forge.capabilities(repo)

    def clone(self, payload: dict[str, Any]) -> dict[str, Any]:
        """The repository's history, as a bundle, with nothing left behind.

        The tree is removed before the response is composed rather than on a
        later `close`, because there is no later: these routes hold no state, so
        a caller that dies mid-request costs the broker nothing.

        There is no `depth`, and this is a property of the transport rather than
        an omission. `git bundle create` in a shallow repository succeeds and
        writes a bundle whose boundary revisions name parents the bundle does
        not carry; cloning it fails with "remote did not send all necessary
        objects". Naming a `branch` is the size control that does work, because
        it makes the clone single-branch.
        """
        bound = self._bind(payload)
        branch = payload.get("branch")
        branch = validate_branch(branch) if branch is not None else None
        if payload.get("depth") is not None:
            raise WorkspaceError(
                "history is transferred as a bundle, which cannot carry a "
                "shallow boundary. Name a `branch` to fetch one line of "
                "development instead."
            )
        bound.ensure()
        git = bound.git

        root = self._scratch("clone")
        bundle = root.parent / f"{root.name}.bundle"
        try:
            argv = ["clone", "--quiet", "--no-recurse-submodules"]
            if branch is not None:
                argv += ["--single-branch", "--branch", branch]
            argv += [bound.forge.clone_url(bound.repo), "."]
            git(root, *argv)
            self._enforce_ceiling(root, bound.repo)
            if branch is None:
                branch = self._default_branch(git, root)
            git(root, "checkout", "--force", "-B", branch, f"origin/{branch}")
            head = git(root, "rev-parse", "HEAD").stdout.strip()
            # `HEAD` as well as the branch, and not redundantly: a bundle
            # written from a named branch alone carries no HEAD ref, and a clone
            # from it lands with an unborn HEAD and nothing checked out. The
            # reader then holds a repository whose log says it has no revisions.
            git(root, "bundle", "create", str(bundle), "HEAD", branch)
            size = bundle.stat().st_size
            if size > self.max_bundle_bytes:
                raise WorkspaceError(
                    f"{bound.repo}'s history is {size} bytes, over the "
                    f"{self.max_bundle_bytes}-byte ceiling. Name a `branch` to "
                    "fetch one line of development.",
                    status=413,
                    code="BUNDLE_TOO_LARGE",
                )
            blob = base64.b64encode(bundle.read_bytes()).decode("ascii")
        finally:
            bundle.unlink(missing_ok=True)
            _remove_tree(root)
        return bound.stamp(
            {
                "branch": branch,
                "revision": head,
                "size": size,
                "bundleBase64": blob,
            }
        )

    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Take the caller's revisions as a bundle and put them on the remote.

        Five checks stand between the bundle and the remote, and each one exists
        because the objects came from the sandbox:

        The branch must not be the target. Every ancestry check below passes for
        a publish onto the branch the copy was cloned from, because that is a
        fast-forward; none of them can see that the branch being fast-forwarded
        is the shared one.

        The bundle must carry exactly the branch it claims. A bundle holding a
        second ref would publish something the caller did not declare, and a
        fetch of one ref would leave the rest unmentioned in the answer.

        Its tip must descend from the revision the caller was handed by `clone`.
        That is what makes this an extension of known history rather than a
        replacement of it.

        The target branch's current tip must also be an ancestor, checked after
        the fetch that learns it. Between `clone` and `publish` somebody else may
        have pushed, and without this the caller's branch would silently discard
        that work.

        And nothing is ever checked out. The scratch repository is fetched into
        and pushed from, never materialised into a working copy, so no
        `.gitattributes`, hook, or `.gitmodules` among the incoming objects has
        anything to act on.
        """
        bound = self._bind(payload)
        branch = validate_branch(payload.get("branch"))
        target = validate_branch(payload.get("target"), "target")
        base_revision = validate_revision(payload.get("baseRevision"))
        if branch == target:
            # The three ancestry checks below all pass for a publish onto the
            # branch it was cloned from -- it is a fast-forward, which is
            # exactly what they are there to require. What they cannot see is
            # that the branch being fast-forwarded is the shared one. The
            # sandbox client refuses this before it builds the bundle; the
            # broker does not trust it to, for the same reason validate_branch
            # runs twice.
            raise WorkspaceError(
                f"branch and target are both {branch}, so this publish would "
                "write to the branch it was cloned from. Publish a branch of "
                "your own and open a proposal onto this one.",
                status=409,
                code="TARGET_IS_BRANCH",
            )
        raw = payload.get("bundleBase64")
        if not isinstance(raw, str) or not raw:
            raise WorkspaceError("bundleBase64 must be a base64 bundle")
        try:
            blob = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001 - binascii.Error and TypeError
            raise WorkspaceError("bundleBase64 is not valid base64") from exc
        if len(blob) > self.max_bundle_bytes:
            raise WorkspaceError(
                f"the bundle is {len(blob)} bytes, over the "
                f"{self.max_bundle_bytes}-byte ceiling",
                status=413,
                code="BUNDLE_TOO_LARGE",
            )
        bound.ensure()
        git = bound.git

        root = self._scratch("publish")
        bundle = root.parent / f"{root.name}.bundle"
        try:
            bundle.write_bytes(blob)
            git(root, "init", "--quiet")
            git(root, "remote", "add", "origin", bound.forge.clone_url(bound.repo))
            # The target first, so the ancestry checks below have something to
            # be about.
            git(root, "fetch", "--quiet", "--no-tags", "origin", target)
            remote_target = git(root, "rev-parse", "FETCH_HEAD").stdout.strip()

            # Then the branch itself, when the remote already has it. A second
            # publish onto a branch this caller opened earlier carries
            # prerequisites that sit on that branch and nowhere near the
            # target, so fetching only the target leaves the bundle unreadable
            # -- which reaches the caller as a git failure rather than as an
            # answer about their revisions.
            existing_head = ""
            existing = git(
                root, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}",
                check=False,
            )
            if existing.returncode == 0:
                existing_head = (existing.stdout or "").split("\t", 1)[0].strip()
                git(
                    root, "fetch", "--quiet", "--no-tags", "origin",
                    f"refs/heads/{branch}",
                )

            # Before the bundle is read, because a rewritten target is also why
            # reading it fails: the bundle's prerequisites sit on the revision
            # this copy was cloned at, and if that revision is gone the unbundle
            # refuses first and the caller gets a git failure instead of the
            # reason for it.
            #
            # Only on a branch's first publish, and that is the whole subtlety.
            # `baseRevision` means "what this bundle builds on", which is a
            # revision of the target the first time and the caller's own last
            # published tip every time after -- and that tip is on the branch,
            # never on the target. Asking this question of a second publish
            # refuses every one of them.
            #
            # The direction is base-under-target, not target-under-tip. The
            # other way round demands that the bundle contain everything on the
            # target, which is to say that a topic branch be rebased onto the
            # tip of the shared branch before every publish -- so any push to
            # the target by anyone, between the clone and the publish, refuses a
            # change that would have merged cleanly. On a shared branch that is
            # most of them, and the refusal it handed back said to clone again,
            # which is the one operation that discards the work.
            #
            # What this direction catches is the case the message is actually
            # about: the target was rewritten rather than advanced, so there is
            # nothing to fast-forward from and cloning again is the right
            # advice. An ordinary advance leaves the base an ancestor and
            # passes; the change then opens as a proposal with a base behind the
            # tip, which is a rebase on the forge and not an error here.
            if not existing_head and not self._is_ancestor(
                git, root, base_revision, remote_target
            ):
                raise WorkspaceError(
                    f"{target} no longer contains {base_revision[:12]}, the "
                    "revision this copy was cloned at, so it was rewritten "
                    "rather than advanced. Clone again and reapply the change.",
                    status=409,
                    code="BASE_MOVED",
                )

            listed = git(root, "bundle", "list-heads", str(bundle)).stdout
            heads = [
                (line.split(" ", 1)[0].strip(), line.split(" ", 1)[1].strip())
                for line in listed.splitlines()
                if " " in line
            ]
            refs = [ref for _, ref in heads]
            wanted = {f"refs/heads/{branch}", branch}
            if len(refs) != 1 or refs[0] not in wanted:
                raise WorkspaceError(
                    f"the bundle carries {refs or 'no refs'}; it must carry "
                    f"exactly refs/heads/{branch}"
                )
            # `unbundle` rather than `fetch <path>`, and the difference is not
            # stylistic: a fetch from a local path is git's `file` transport,
            # which `GIT_ALLOW_PROTOCOL` refuses on every door this executor
            # opens. That refusal is load-bearing -- the credential-proxy
            # environment says why, and the short form is that `file` is what
            # makes `--upload-pack=<cmd>` executable -- so the way through is a
            # subcommand that needs no transport, not a wider allowlist. This
            # one hands the pack to `index-pack` directly. Found live: publish
            # answered 502 with `transport 'file' not allowed` while every
            # read verb passed, because reading is the direction that travels
            # as `bundle create` and never fetches anything.
            #
            # It verifies the bundle's prerequisites exactly as the fetch did,
            # so a bundle that does not build on what this repository already
            # has still fails here rather than downstream. What it does not do
            # is write a ref, so the tip comes from `list-heads` -- already
            # parsed above to check the bundle carries one branch -- and the
            # ref is made by hand.
            tip = heads[0][0]
            git(root, "bundle", "unbundle", str(bundle))
            git(root, "update-ref", _INCOMING, tip)

            if not self._is_ancestor(git, root, base_revision, tip):
                raise WorkspaceError(
                    f"the bundle's tip {tip[:12]} does not descend from "
                    f"{base_revision[:12]}, the revision this copy was cloned at",
                    status=409,
                    code="NOT_FAST_FORWARD",
                )
            if existing_head and not self._is_ancestor(git, root, existing_head, tip):
                raise WorkspaceError(
                    f"{branch} exists on the remote at {existing_head[:12]} and "
                    "the bundle does not build on it",
                    status=409,
                    code="BRANCH_DIVERGED",
                )
            git(root, "push", "origin", f"{_INCOMING}:refs/heads/{branch}")
        finally:
            bundle.unlink(missing_ok=True)
            _remove_tree(root)
        return bound.stamp({"branch": branch, "revision": tip})

    @staticmethod
    def _is_ancestor(
        git: Callable, root: Path, ancestor: str, descendant: str
    ) -> bool:
        if not ancestor:
            return False
        result = git(
            root, "merge-base", "--is-ancestor", ancestor, descendant, check=False
        )
        return result.returncode == 0

    # ---- collaboration verbs -------------------------------------------

    def _forge_verb(self, verb: str, payload: dict[str, Any]) -> dict[str, Any]:
        bound = self._bind(payload)
        method = getattr(bound.forge, verb.replace("-", "_"))
        return bound.stamp(method(bound.api, bound.repo, payload))

    def proposal_create(self, payload):
        return self._forge_verb("proposal-create", payload)

    def proposal_list(self, payload):
        return self._forge_verb("proposal-list", payload)

    def proposal_view(self, payload):
        return self._forge_verb("proposal-view", payload)

    def proposal_comment(self, payload):
        return self._forge_verb("proposal-comment", payload)

    def issue_create(self, payload):
        return self._forge_verb("issue-create", payload)

    def issue_list(self, payload):
        return self._forge_verb("issue-list", payload)

    def issue_view(self, payload):
        return self._forge_verb("issue-view", payload)

    def issue_comment(self, payload):
        return self._forge_verb("issue-comment", payload)


# The verbs that leave a mark on the forge, named here so the HTTP layer can
# refuse an unmanaged repository before one of them runs. The classification
# lives beside the route table because that is where a new verb gets added, and
# a verb added to one and not the other is the mistake this placement is meant
# to make loud.
#
# The read verbs are deliberately absent rather than overlooked. `clone`,
# `capabilities` and the four list/view verbs spend the credential too, and they
# stay open for the reason `require_managed_workspace` gives about the content
# workspace's `open`: reading a repository this install does not write to is a
# thing the agent is supposed to be able to do, and `inspect-repository` is
# built on it. The managed list is a write control, not a visibility one.
WRITE_VERBS = frozenset(
    {
        "publish",
        "proposal-create",
        "proposal-comment",
        "issue-create",
        "issue-comment",
    }
)


def route_table(broker: VcsBroker) -> dict[str, Callable[[dict], dict]]:
    """The verbs `POST /v1/vcs/<verb>` dispatches to.

    Hyphens in the URL, underscores in the method names. The dispatcher
    normalises the two, so `proposal-create` and `proposal_create` reach the
    same route and no caller fails on punctuation.
    """
    return {
        "capabilities": broker.capabilities,
        "clone": broker.clone,
        "publish": broker.publish,
        "proposal-create": broker.proposal_create,
        "proposal-list": broker.proposal_list,
        "proposal-view": broker.proposal_view,
        "proposal-comment": broker.proposal_comment,
        "issue-create": broker.issue_create,
        "issue-list": broker.issue_list,
        "issue-view": broker.issue_view,
        "issue-comment": broker.issue_comment,
    }


__all__ = [
    "Binding",
    "VcsBroker",
    "WRITE_VERBS",
    "max_bundle_bytes",
    "route_table",
]
