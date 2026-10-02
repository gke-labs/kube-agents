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
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

import repo_ref
from providers import (
    CliTransport,
    Forge,
    ForgeUnsupported,
    MAX_PAGE_SIZE,
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

# How many open proposals the `advance` check reads off a branch. One would
# settle whether any is open; the rest are read because the second half of the
# check asks whose they are, and a forge that lets two proposals share a source
# branch would otherwise have the answer decided by whichever came back first.
# Not a page to walk: a branch with more than this many open proposals on it is
# not a case this refusal is trying to be exact about.
OPEN_PROPOSALS_ON_A_BRANCH = 10

# The spellings `_short_ref` reads as the bare branch. Wider than
# `repo_ref.BRANCH_REF_PREFIXES` on purpose: it feeds the protected-branch check,
# where reading `heads/main` as `main` refuses more, and the branch verbs. A
# proposal's target is compared through `repo_ref.short_branch` instead, because
# a forge opens the proposal on the name it was given.
_SHORT_REF_PREFIXES = ("refs/heads/", "heads/")

# The ref an incoming bundle is fetched into. Under `refs/vcs/` rather than
# `refs/heads/` so nothing here can be confused with a branch, and so a publish
# of a leftover ref cannot happen by naming a plausible branch.
_INCOMING = "refs/vcs/incoming"

# The namespace every branch this install publishes is named under, and the only
# one `branch-delete` will touch. Every shipped caller derives its branch as
# `platform-agent/<change>-<target>`; a branch outside it is a person's, or
# another tool's, whatever its history says.
AGENT_BRANCH_PREFIX = "platform-agent/"

# How many of a branch's proposals `branch-delete` reads. It needs all of them:
# the one that carried the tip, and every other, since a branch any of them
# shows a person proposed from is not this install's to delete. A history the
# forge answers as truncated is therefore refused rather than judged on its
# first page. A spent agent branch carries one or two, but a flow with a fixed
# name -- one per workload, reused on every alert -- adds one per round, so the
# page is the largest a forge serves in one call, and it is still one call.
PROPOSAL_HISTORY_ON_A_BRANCH = MAX_PAGE_SIZE

# What git prints when the remote answered the delete and said no for good: a
# hook, branch rule or ruleset (`[remote rejected] <ref> (... declined)`), a
# remote that forbids deletes (`... prohibited`), or a credential without the
# right to push (`denied`, HTTP 403). Every run gets the same answer, so it is
# refused as DELETE_REFUSED rather than left to GIT_FAILED, which callers
# retry. Only those reasons: receive-pack also answers a lock race or a
# backend fault as `[remote rejected] <ref> (failed to lock)` and the like,
# and a second attempt clears those.
_REMOTE_REFUSED = re.compile(
    r"\[remote rejected\] [^\n]*\([^)\n]*(declined|prohibited|denied)[^)\n]*\)"
    r"|returned error: 403|Permission to \S+ denied"
)


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


_AUTOMATION_MARKING = re.compile(r"\[[^\]]*\]$")


def _short_ref(branch: str) -> str:
    """`refs/heads/x` and `heads/x` name the branch `x`; compare them as `x`."""
    short = branch.strip()
    for prefix in _SHORT_REF_PREFIXES:
        if short.startswith(prefix):
            return short[len(prefix):]
    return short


def _login_key(login: str) -> str:
    """One spelling for two sources that mark an automation differently.

    A provider's translation strips the marking a forge puts on an automation's
    login before it emits an author; a transport's `whoami` reports whatever the
    credential store holds, marking and all. Compared raw, an install is a
    stranger to its own proposals.

    The marking is therefore not available to tell the two apart, and this
    folds `kube-agents[bot]` onto a human account spelled `kube-agents`: the
    `advance` ownership check above would read that person's open proposal as
    this install's. Getting it back needs the author's bot flag carried through
    `proposal-list`, which no forge in the protocol emits today. The failure is
    permissive and it takes a human registering the install's own App name on
    the same forge, so it is recorded rather than guarded -- guessing from the
    spelling is how the comparison broke in the first place.
    """
    return _AUTOMATION_MARKING.sub("", (login or "").strip()).casefold()


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

    def transport(self) -> Transport:
        if self._built is None:
            self._built = self._transport()
        return self._built

    def api(self, method: str, path: str, **kwargs: Any) -> Any:
        self.ensure()
        return self.transport().api(method, path, **kwargs)

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
        base_branch: str | None = None,
        base_repository: str | None = None,
    ) -> None:
        self.scratch_root = Path(scratch_root)
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self._git_runner = git_runner
        self.base_branch = (base_branch or "").strip()
        # The repository `base_branch` is the proposal base of. Both together
        # pin that one repository's base (`_pinned_base`); `base_branch` alone
        # keeps only its older meaning, a branch no write door may move.
        self.base_repository = (base_repository or "").strip()
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

    def _pinned_base(self, repo: str) -> str | None:
        """The branch every proposal onto `repo` must target, or None."""
        return repo_ref.pinned_base(repo, self.base_branch, self.base_repository)

    def _refuse_off_base(self, repo: str, target: Any) -> str | None:
        """Refuse a proposal target that is not `repo`'s pinned base.

        Request-only, so it runs before a credential is made current or the
        forge is called. Compared exactly once a `refs/heads/` prefix is gone.
        `Main` is another branch, because git branch names are case-sensitive,
        and so is `heads/main`. The base goes in the message because the
        sandbox client keeps only the error text and the code.

        Answers the bare base when the target is it, so the caller sends the
        forge that name rather than the spelling it was given; None when
        nothing is pinned for `repo`.
        """
        base = self._pinned_base(repo)
        if base is None:
            return None
        target = repo_ref.short_branch(validate_branch(target, "target"))
        if target != base:
            raise WorkspaceError(
                f"{repo} takes proposals onto {base} only, the base branch this "
                f"install is configured with, and {target} is not it. Use {base} "
                "as the target.",
                status=409,
                code="TARGET_NOT_BASE",
            )
        return base

    # ---- repository verbs ----------------------------------------------

    def capabilities(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What this install can do with this repository, before anything is spent.

        Answered without making a credential current or touching the network. A
        caller that discovers the gap by failing halfway through a publish has
        already written the revision it cannot deliver.

        `baseBranch` is the repository's pinned base, or None when this install
        pins none for it. It is the read a caller with no clone uses to learn
        which branch its proposals must target.
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
                "baseBranch": None,
            }
        answer = forge.capabilities(repo)
        answer["baseBranch"] = self._pinned_base(repo)
        return answer

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

        With no `branch` named, a repository with a pinned base is checked out
        on that base rather than on the remote's default: the copy is what a
        proposal is cut from, and the base is the only target the proposal
        will be accepted onto. A pinned base the remote does not have is
        refused by name. `baseBranch` in the answer is the pinned base, or None.
        """
        bound = self._bind(payload)
        base = self._pinned_base(bound.repo)
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
            if branch is None and base is not None:
                present = git(
                    root, "rev-parse", "--verify", "--quiet",
                    f"refs/remotes/origin/{base}", check=False,
                )
                if present.returncode != 0:
                    raise WorkspaceError(
                        f"{bound.repo} has no branch {base}, the base branch "
                        "this install is configured with. Proposals onto this "
                        "repository can only target that branch, so it has to "
                        "exist on the remote first.",
                        status=409,
                        code="BASE_BRANCH_MISSING",
                    )
                branch = base
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
                "baseBranch": base,
            }
        )

    def publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Take the caller's revisions as a bundle and put them on the remote.

        Six checks stand between the bundle and the remote, and each one exists
        because the objects came from the sandbox:

        The branch must not be the remote's default branch, whatever `target`
        says. Every ancestry check below passes for a fast-forward of the
        shared branch, and both `branch` and `target` are fields the sandbox
        chose -- so a guard that only compared the two was defeated by naming
        any other existing branch as the target. The default branch is the one
        fact about "shared" the broker can learn from the remote itself.

        The branch must not be the target either. That closes the same door for
        a copy cloned from a branch that is not the default, where the broker
        has nothing to check against but what the caller declared. `advance`
        waives the declared half of that pair and nothing else -- it is how a
        caller says the branch it cloned is a proposal branch it is here to add
        to, which is the one case where writing to it is the whole point.

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
        cloned_from = payload.get("clonedFrom")
        if cloned_from is not None:
            cloned_from = validate_branch(cloned_from, "clonedFrom")
        # The one legitimate reason to write to the branch this copy was cloned
        # from: the copy was taken *of* a proposal branch in order to add to it.
        # A caller revising an open proposal has to clone the branch the
        # proposal is on -- there is nowhere else its revisions are -- and every
        # other check still applies to it, the default-branch refusal below
        # included. The field is the caller saying which of the two situations
        # this is, and a caller that omits it gets the refusal.
        advance = bool(payload.get("advance"))
        if cloned_from and branch == cloned_from and not advance:
            # The client says so itself: this is the branch the copy was
            # cloned from, whatever `target` names. A client that lies here
            # gains nothing it could not get by omitting the field, so this
            # is defence in depth for a confused caller, not a control
            # against a hostile one -- the default-branch check below and the
            # forge's own branch protection are those.
            raise WorkspaceError(
                f"{branch} is the branch this copy was cloned from, so this "
                "publish would write to it directly. Publish a branch of your "
                "own and open a proposal onto it.",
                status=409,
                code="CLONED_BRANCH",
            )
        if branch == target:
            # The three ancestry checks below all pass for a publish onto the
            # branch it was cloned from -- it is a fast-forward, which is
            # exactly what they are there to require. What they cannot see is
            # that the branch being fast-forwarded is the shared one. The
            # sandbox client refuses this before it builds the bundle; the
            # broker does not trust it to, for the same reason validate_branch
            # runs twice. On its own this check is not enough -- `target` is
            # the caller's field too -- which is why the default-branch check
            # below asks the remote rather than the request.
            raise WorkspaceError(
                f"branch and target are both {branch}, so this publish would "
                "write to the branch it was cloned from. Publish a branch of "
                "your own and open a proposal onto this one.",
                status=409,
                code="TARGET_IS_BRANCH",
            )
        if not advance:
            # A first publish is the start of a new proposal, so its target
            # must be the repository's pinned base, the only one
            # `proposal-create` will accept. Refused here, before a credential
            # is spent, rather than after the branch is already on the remote.
            #
            # A later round (`advance`) is exempt. It adds to a proposal that
            # already exists, and its target is that proposal's, which no verb
            # can move. Refusing it would block every follow-up on a proposal
            # a person opened onto another branch while leaving the proposal
            # exactly where it is.
            self._refuse_off_base(bound.repo, target)
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
            self._refuse_protected(
                git, root, branch,
                "Publish a branch of your own and open a proposal onto it.",
            )
            if advance:
                # The waived refusal, checked against the forge rather than
                # taken on the caller's word alone. `advance` says one thing --
                # this copy was cloned *of* a proposal branch in order to add
                # to it -- and an open proposal whose source is this branch is
                # that thing, read off the forge.
                #
                # A bar, not a proof, and worth being exact about which. The
                # same caller can open that proposal first: `proposal-create`
                # is on the same route table, gated on the same managed list a
                # caller already passed to reach `publish`. So the sequence
                # this stops -- clone `release-1.2`, commit, `publish --target
                # main --advance`, the live incident the refusal was added for
                # -- is not made impossible; it is made to cost a pull request
                # under the install's own name, open on the forge for anyone
                # to see, and it stops being something a worker does by
                # mistake because the refusal text named a flag. Without the
                # check the field was simply that flag.
                #
                # "Under the install's own name" is a claim about the author,
                # so the author is what is compared -- otherwise the ordinary
                # `release-1.2` back-merge, open on the forge under somebody
                # else's name, clears the bar and the cost is zero. It is
                # compared only where both halves can be read: a credential
                # that cannot introspect itself leaves the weaker bar, which is
                # why this is still a bar and not a proof.
                #
                # The refusals that do not come from the request -- the
                # remote's default branch, the protected names, the base
                # override, `run/**` -- stand regardless, which is the part
                # that is a proof.
                #
                # After the default-branch check, not before it. That refusal is
                # the one the broker establishes for itself, and it must stay
                # the answer a caller gets for trying `advance` on `main`.
                self._require_open_proposal(bound, branch)
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

    def branch_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Whether `branch` exists on the remote, and at which revision.

        The read a caller needs before reusing a branch name. Branch names here
        are derived from the change, so they recur, and a forge that keeps a
        head branch after its proposal is squash-merged or closed -- the
        commonest default -- leaves the old tip in the way of a branch cut afresh from the
        base. Without this, "the branch is gone" and "the branch is still there"
        looked the same from the sandbox, and the second was found out as
        `BRANCH_DIVERGED` after the whole change had been written.

        git against the remote rather than a forge call, like `publish`: which
        refs a remote holds is a question every forge answers the same way.
        """
        bound = self._bind(payload)
        branch = _short_ref(validate_branch(payload.get("branch")))
        bound.ensure()
        root = self._scratch("branch")
        try:
            revision = self._remote_tip(bound, root, branch)
        finally:
            _remove_tree(root)
        return bound.stamp(
            {"branch": {"name": branch, "exists": bool(revision), "revision": revision or None}}
        )

    def branch_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Delete a spent branch: one whose proposal is done with and whose tip it carried.

        The write that lets a derived branch name be used twice. Every refusal
        below is about keeping it to exactly that, because a delete is the one
        write in this table that takes something off the forge rather than
        adding to it.

        The branch must be under `AGENT_BRANCH_PREFIX`. Outside it the branch
        is a person's, or another tool's, and nothing this install learns about
        its history makes it this install's to remove. The prefix is a
        convention, not a lock -- anyone who can push can push under it -- so
        the proposal that carried the tip must also be this install's: opened
        by the credential's own login, from this repository rather than a fork.
        So must every other proposal the branch's history lists: a branch a
        person ever proposed from is theirs too, whatever this install later
        opened on it. All of them, so a history the forge answers in more than
        one page is refused rather than judged on the first. A credential that cannot name itself leaves the prefix and the
        same-repository rule as the bar, the same weaker bar `advance` settles for and for the reason
        `_viewer` gives; one whose lookup failed is refused, since that
        silence is an outage rather than an answer.

        It must not be protected, by the same rule `publish` applies -- the
        remote's own default branch included, which is the one fact about
        "shared" the broker learns from the remote rather than the request.

        No proposal on it may be open. An open proposal is somebody's work in
        review, and its branch is that work.

        And its tip must be the last revision of a merged or closed proposal
        from it. That is what "spent" means, checked rather than claimed: a
        branch that moved on after its proposal closed holds revisions no
        proposal carried, and deleting it would lose them. On the shipped forge
        the revision the proposal carried stays reachable from the proposal
        itself, so what this removes is a name, not work.

        These gates hold against a mistake, not against their own caller: the
        same caller can open a proposal from a branch with `proposal-create`
        and close it, which makes the tip carried and the carrier this
        install's -- the bar `advance` states for itself. What that caller
        still cannot do is lose work: the revision it made carried stays
        reachable from the proposal it opened, and a branch a person proposed
        from is refused above whatever this install opened on it since.

        `revision` is the tip the caller last read, from `branch-view`, and the
        delete is conditional on it: a sibling that published to the same name
        in between wins, and this refuses rather than removing what it pushed.
        A branch that is already gone is not an error -- the caller wanted it
        gone -- and is answered with `deleted: false`.
        """
        bound = self._bind(payload)
        branch = _short_ref(validate_branch(payload.get("branch")))
        expected = validate_revision(payload.get("revision"), "revision")
        if not branch.startswith(AGENT_BRANCH_PREFIX):
            raise WorkspaceError(
                f"{branch} is not under {AGENT_BRANCH_PREFIX}, the namespace this "
                "install publishes in, so it is not this install's to delete.",
                status=409,
                code="BRANCH_NOT_OURS",
            )
        if "proposal-list" not in getattr(bound.forge, "verbs", ()):
            raise ForgeUnsupported(
                f"{bound.forge.name} cannot list proposals in this install, so "
                f"whether {branch} is spent cannot be established and it is not "
                "deleted."
            )
        bound.ensure()
        git = bound.git
        root = self._scratch("branch")
        try:
            git(root, "init", "--quiet")
            git(root, "remote", "add", "origin", bound.forge.clone_url(bound.repo))
            self._refuse_protected(
                git, root, branch, "Deleting it is not something this verb does."
            )
            answer = bound.forge.proposal_list(
                bound.api,
                bound.repo,
                {"state": "all", "source": branch, "limit": PROPOSAL_HISTORY_ON_A_BRANCH},
            )
            proposals = answer.get("proposals") or []
            # Asked on its own rather than read off the history: that is one
            # page, newest first, and an open proposal older than it -- onto a
            # second base, say -- would fall off the end and be closed by the
            # delete.
            opened = bound.forge.proposal_list(
                bound.api, bound.repo, {"state": "open", "source": branch, "limit": 1}
            )
            still_open = [
                item for item in [*(opened.get("proposals") or []), *proposals]
                if item.get("state") == "open"
            ]
            if still_open:
                named = still_open[0].get("url") or "#%s" % (still_open[0].get("number"),)
                raise WorkspaceError(
                    f"{branch} carries an open proposal, {named}. Its branch is "
                    "the work under review, so it is not deleted.",
                    status=409,
                    code="OPEN_PROPOSAL",
                )
            # A proposal is on a branch as its target too: somebody stacked
            # work on it. Deleting a proposal's target closes it, and that
            # proposal is somebody's work in review, not this install's to end.
            stacked = bound.forge.proposal_list(
                bound.api, bound.repo, {"state": "open", "target": branch, "limit": 1}
            )
            onto = [
                item for item in stacked.get("proposals") or []
                if item.get("state", "open") == "open"
            ]
            if onto:
                named = onto[0].get("url") or "#%s" % (onto[0].get("number"),)
                raise WorkspaceError(
                    f"an open proposal, {named}, targets {branch}. Deleting the "
                    "branch would close that proposal, which is somebody's work "
                    "in review, so the branch is not this install's to delete.",
                    status=409,
                    code="BRANCH_NOT_OURS",
                )
            tip = self._remote_tip(bound, root, branch)
            if not tip:
                return bound.stamp(
                    {"branch": {"name": branch, "deleted": False, "revision": None}}
                )
            if tip != expected:
                raise WorkspaceError(
                    f"{branch} is at {tip[:12]} on the remote, not {expected[:12]}, "
                    "so it moved after it was read. Read it again before deleting it.",
                    status=409,
                    code="BRANCH_MOVED",
                )
            carriers = [
                item for item in proposals if str(item.get("sourceRevision") or "") == tip
            ]
            if not carriers:
                raise WorkspaceError(
                    f"{branch} is at {tip[:12]}, which no merged or closed proposal "
                    "from it carried, so it holds revisions that would be lost. "
                    "It is not deleted.",
                    status=409,
                    code="NOT_SPENT",
                )
            self._refuse_a_carrier_not_ours(
                bound, branch, carriers, proposals, complete=not answer.get("truncated")
            )
            # Conditional on the tip just compared, so a publish that lands
            # between the read and this push is refused by the remote rather
            # than silently undone.
            argv = (
                "push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{tip}",
                "origin", f":refs/heads/{branch}",
            )
            pushed = git(root, *argv, check=False)
            if pushed.returncode != 0:
                # A lost lease and a push that failed for any other reason
                # exit alike. The tip tells them apart: a caller told
                # BRANCH_MOVED reads again, one told GIT_FAILED retries the
                # same delete against a branch that is no longer spent. Gone
                # is neither: the push reported failure after the remote took
                # it, or something else removed the branch, and either way the
                # caller has what it asked for -- answered as for a branch
                # already gone.
                now = self._remote_tip(bound, root, branch)
                if not now:
                    return bound.stamp(
                        {"branch": {"name": branch, "deleted": False, "revision": None}}
                    )
                if now != tip:
                    raise WorkspaceError(
                        f"{branch} moved to {now[:12]} while it was "
                        f"being deleted at {tip[:12]}, so the delete was refused "
                        "and whatever moved it is kept.",
                        status=409,
                        code="BRANCH_MOVED",
                    )
                refused = _REMOTE_REFUSED.search(pushed.stderr or "")
                if refused:
                    raise WorkspaceError(
                        f"the remote refused to delete {branch} "
                        f"({refused.group(0).strip()[:200]}): a branch rule or "
                        "hook covers it, or this install's credential may not "
                        "delete branches there. Asking again gets the same "
                        "answer, so the branch stays and its name is not usable.",
                        status=409,
                        code="DELETE_REFUSED",
                    )
                raise subprocess.CalledProcessError(
                    pushed.returncode, ["git", *argv], pushed.stdout, pushed.stderr
                )
        finally:
            _remove_tree(root)
        return bound.stamp({"branch": {"name": branch, "deleted": True, "revision": tip}})

    def _remote_tip(self, bound: Binding, root: Path, branch: str) -> str:
        """The revision `branch` is at on the remote, or "" when it has none.

        A failed read is not an empty one. `ls-remote --exit-code` exits 2 for
        "no such ref" and something else for a remote it could not ask, and
        answering "" for the second would tell a caller a branch is gone when
        the forge simply did not answer.
        """
        git = bound.git
        if not (root / ".git").exists():
            git(root, "init", "--quiet")
            git(root, "remote", "add", "origin", bound.forge.clone_url(bound.repo))
        listed = git(
            root, "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}",
            check=False,
        )
        if listed.returncode == 2:
            return ""
        if listed.returncode != 0:
            raise WorkspaceError(
                f"could not read {branch} from the remote: "
                f"{(listed.stderr or '').strip() or 'git exited ' + str(listed.returncode)}",
                status=502,
                code="FORGE_CALL_FAILED",
            )
        # `ls-remote` matches a pattern against the tail of each ref name, so
        # `refs/heads/foo/refs/heads/<branch>` answers too, and sorts first.
        wanted = f"refs/heads/{branch}"
        for line in (listed.stdout or "").splitlines():
            revision, _, name = line.partition("\t")
            if name.strip() == wanted:
                return revision.strip()
        return ""

    def _refuse_a_carrier_not_ours(
        self,
        bound: Binding,
        branch: str,
        carriers: list[dict[str, Any]],
        history: list[dict[str, Any]],
        complete: bool = True,
    ) -> None:
        """Refuse to delete a branch whose spent proposal this install did not open.

        `carriers` are the proposals from `branch` that carried its tip. One of
        them has to be from this repository and, when the credential can name
        itself, by this install. `history` is every proposal listed from it,
        and when the credential can name itself none of those may be anybody
        else's -- which one page cannot show when there are more, so an
        incomplete `history` is refused too. See `branch_delete` for why each.
        """
        here = bound.repo.casefold()
        ours = [
            item for item in carriers
            if str(item.get("sourceRepo") or "").casefold() == here
        ]
        if not ours:
            raise WorkspaceError(
                f"the proposal that carried {branch}'s tip was not opened from "
                f"{bound.repo}, so the branch is not this install's to delete.",
                status=409,
                code="BRANCH_NOT_OURS",
            )
        try:
            viewer = self._viewer(bound)
        except WorkspaceError as failed:
            raise WorkspaceError(
                f"asking the forge who this credential is failed, so whether "
                f"{branch} is this install's could not be established: {failed}. "
                "Retry; the branch has not been deleted.",
                status=502,
                code="FORGE_CALL_FAILED",
            ) from failed
        if not viewer:
            return
        authors = {str(item.get("author") or "") for item in ours}
        authors.discard("")
        if _login_key(viewer) not in {_login_key(author) for author in authors}:
            named = ", ".join(sorted(authors)) or "an unnamed author"
            raise WorkspaceError(
                f"the proposal that carried {branch}'s tip is {named}'s, not this "
                f"install's ({viewer}), so the branch is not this install's to "
                "delete.",
                status=409,
                code="BRANCH_NOT_OURS",
            )
        if not complete:
            raise WorkspaceError(
                f"{branch} carried at least {len(history)} proposals, a full "
                "page, so whether every one of them was this install's cannot be "
                "established and the branch is not deleted. This is the length "
                "of its history, not a proposal found to be somebody else's.",
                status=409,
                code="BRANCH_NOT_OURS",
            )
        others = [
            item for item in history
            if _login_key(str(item.get("author") or "")) != _login_key(viewer)
        ]
        if others:
            first = others[0]
            named = first.get("url") or "#%s" % (first.get("number"),)
            raise WorkspaceError(
                f"{branch} also carried {named}, "
                f"{first.get('author') or 'an unnamed author'}'s rather than this "
                f"install's ({viewer}), so the branch is not only this install's "
                "and is not deleted.",
                status=409,
                code="BRANCH_NOT_OURS",
            )

    def _refuse_protected(
        self, git: Callable, root: Path, branch: str, advice: str
    ) -> None:
        """Refuse a branch no write door may move, whatever the request says.

        Which branch the remote calls its default, from the remote and not from
        the request. The broker enforces protected branch policy across all
        write doors: main, master, production, the remote default branch, any
        operator-configured base override, and any run/** branch. `publish`
        refuses a direct push to one and `branch-delete` refuses to remove one.
        `root` must already have `origin`.
        """
        default = self._default_branch_of_remote(git, root)
        protected_branches = {"main", "master", "production"}
        if default:
            protected_branches.add(default.casefold())
        base_override = (
            self.base_branch
            or os.environ.get("CREDENTIAL_PROXY_BASE_BRANCH", "").strip()
            or os.environ.get("GITOPS_BASE_BRANCH", "").strip()
        )
        if base_override:
            protected_branches.add(_short_ref(base_override).casefold())

        normalized_branch = _short_ref(branch)
        if (
            normalized_branch.casefold() in protected_branches
            or normalized_branch.casefold().startswith("run/")
        ):
            raise WorkspaceError(
                f"{branch} is a protected, default, or run branch. {advice}",
                status=409,
                code="PROTECTED_BRANCH",
            )

    @staticmethod
    def _default_branch_of_remote(git: Callable, root: Path) -> str:
        """The branch the remote's HEAD points at, or "" if it says nothing.

        `ls-remote --symref` prints `ref: refs/heads/<name>\tHEAD` first when
        the remote advertises a symbolic HEAD. A remote that advertises none --
        an empty repository, or a server that hides it -- yields "", and the
        caller treats that as "no default to protect" rather than as a
        refusal, because there is nothing to compare against.
        """
        listed = git(root, "ls-remote", "--symref", "origin", "HEAD", check=False)
        for line in (listed.stdout or "").splitlines():
            if line.startswith("ref: refs/heads/") and line.rstrip().endswith("HEAD"):
                return line[len("ref: refs/heads/"):].split("\t", 1)[0].strip()
        return ""

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

    def _require_open_proposal(self, bound: Binding, branch: str) -> None:
        """Refuse unless `branch` already carries an open proposal.

        One extra read on the `advance` path only, which is the second and
        later rounds of a proposal the caller already opened -- not the first
        publish of anything. What it establishes is that such a proposal is
        open on the forge and that this install is the one who opened it, the
        second only where the credential can say who it is and the forge names
        the proposal's author. It does not establish that the install opened it
        *before* this request: the same caller can call `proposal-create` one
        verb earlier. See the comment at the call site for what that does and
        does not buy.

        A forge that does not serve `proposal-list` is left alone. `publish`
        holds no forge otherwise -- it is git against a URL, which is what makes
        the seam a seam -- and a forge with no proposals has no branch this
        could be the second round of, so there is nothing for the check to
        establish. The refusals that do not come from the request, the
        default-branch one above chief among them, still stand there.
        """
        if "proposal-list" not in getattr(bound.forge, "verbs", ()):
            return
        answer = bound.forge.proposal_list(
            bound.api,
            bound.repo,
            {"state": "open", "source": branch, "limit": OPEN_PROPOSALS_ON_A_BRANCH},
        )
        proposals = answer.get("proposals") or []
        if not proposals:
            raise WorkspaceError(
                f"`advance` says {branch} is a proposal branch this copy was "
                "cloned in order to add to, but no open proposal on this "
                "repository has it as its source. Publish a branch of your own "
                "and open a proposal onto it.",
                status=409,
                code="CLONED_BRANCH",
            )
        # Whose proposal it is, when both halves of the question can be
        # answered. A long-lived branch carrying somebody else's open proposal
        # -- the `release-1.2` back-merge every GitOps repository has one of --
        # otherwise satisfies the bar with nothing under this install's name at
        # all, which is the incident with an extra step rather than a cost.
        try:
            viewer = self._viewer(bound)
        except WorkspaceError as failed:
            # A lookup that did not happen is not a credential that cannot
            # say, and settling for the weaker bar on it would drop the
            # ownership half of this check on exactly the branch it was added
            # for. Refused with the transport's own code rather than
            # `CLONED_BRANCH`: what is known is that the question could not be
            # asked, and reporting that as "the proposal is somebody else's"
            # would be a guess that sends the caller to rename a branch over
            # what a retry fixes.
            raise WorkspaceError(
                f"`advance` says {branch} is a proposal branch this copy was "
                "cloned in order to add to, and an open proposal on it exists, "
                "but asking the forge who this credential is failed, so whether "
                "that proposal is this install's could not be established: "
                f"{failed}. Retry; the branch has not been moved.",
                status=502,
                code="FORGE_CALL_FAILED",
            ) from failed
        authors = {str(item.get("author") or "") for item in proposals}
        authors.discard("")
        keys = {_login_key(author) for author in authors}
        if viewer and authors and _login_key(viewer) not in keys:
            # Named as the forge spells them, not as they were compared: the
            # normalisation is this broker's business, and a refusal that
            # reports a login nobody can search for is a worse refusal.
            named = ", ".join(sorted(authors))
            raise WorkspaceError(
                f"`advance` says {branch} is a proposal branch this copy was "
                f"cloned in order to add to, but the open proposal on it is "
                f"{named}'s, not this install's ({viewer}). Adding to it moves "
                "a branch whose proposal this install does not own. Publish a "
                "branch of your own and open a proposal onto it.",
                status=409,
                code="CLONED_BRANCH",
            )

    def _viewer(self, bound: Binding) -> str:
        """The login this credential authenticates as, or "" when it cannot say.

        Empty is a real answer and not a failure: `whoami` is documented to
        return it for a credential that cannot introspect itself, and a forge
        that declares no transport this broker builds -- the directory-backed
        one the tests run against -- has nowhere to ask. The caller treats "" as
        "do not compare", which leaves the weaker bar in place rather than
        refusing every `advance` on such an install.

        Which is why only the second of those is caught here. A forge with no
        transport cannot answer the question at all, and never will; a
        transport whose call failed would have answered, and its silence is an
        outage wearing the same clothes. Turning that into "" is how the weaker
        bar arrives on an install that was meant to have the stronger one, so
        it is left to the caller to refuse.
        """
        try:
            return bound.transport().whoami()
        except ForgeUnsupported:
            return ""

    def _forge_verb(
        self, verb: str, payload: dict[str, Any], pinned_target: bool = False
    ) -> dict[str, Any]:
        bound = self._bind(payload)
        if pinned_target:
            # Before the method is looked up and before `bound.api`, so a
            # refused target makes no forge call and spends no credential.
            base = self._refuse_off_base(bound.repo, payload.get("target"))
            if base is not None:
                payload = {**payload, "target": base}
        method = getattr(bound.forge, verb.replace("-", "_"))
        return bound.stamp(method(bound.api, bound.repo, payload))

    def proposal_create(self, payload):
        return self._forge_verb("proposal-create", payload, pinned_target=True)

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

    def proposal_update(self, payload):
        # No adapter moves a proposal's target today, so this is defensive: a
        # `target` that reaches one is held to the same pinned base as
        # `proposal-create`, and the rule stays true the day an adapter learns
        # to retarget.
        return self._forge_verb(
            "proposal-update", payload,
            pinned_target=payload.get("target") is not None,
        )

    def proposal_close(self, payload):
        return self._forge_verb("proposal-close", payload)

    def proposal_commits(self, payload):
        return self._forge_verb("proposal-commits", payload)

    def proposal_acknowledge(self, payload):
        return self._forge_verb("proposal-acknowledge", payload)

    def issue_update(self, payload):
        return self._forge_verb("issue-update", payload)

    def issue_close(self, payload):
        return self._forge_verb("issue-close", payload)

    def label_ensure(self, payload):
        return self._forge_verb("label-ensure", payload)

    def identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Who this credential is on this forge, and whether a login may write.

        A broker verb rather than a forge verb because half of it is a property
        of the transport -- how the call is authenticated -- and not of the
        API: a CLI reads its login out of the credential store, an HTTP client
        asks the current-user route. The other half, `canWrite`, is the forge's
        normalised answer to a question every forge spells differently. Both
        exist so the agent-side policy that separates the agent's own
        proposals and comments from a stranger's stays above the provider.

        `login` in the payload asks about that login; absent, about the
        credential itself. `bot` beside it says the login is an automation's,
        as the forge reported it on the comment being asked about: the
        translation strips the App marking off every author it emits, so the
        caller hands the fact back rather than a spelling it does not know.
        """
        bound = self._bind(payload)
        bound.ensure()
        transport = bound.transport()
        viewer = transport.whoami()
        login = payload.get("login")
        if login is not None and not isinstance(login, str):
            raise WorkspaceError("login must be a string")
        bot = payload.get("bot", False)
        if not isinstance(bot, bool):
            raise WorkspaceError("bot must be true or false")
        # `canWrite` is answered for a login the caller named, never for the
        # credential itself: on the shipped forge an App's own bot login is
        # not a collaborator, so the permission endpoint answers 404 for it
        # and would report the account that just pushed as unable to write. The
        # callers that ask this ask about comment authors; the credential's own
        # standing is what `publish` proves by doing it.
        subject = (login or "").strip()
        can_write = (
            bound.forge.can_write(bound.api, bound.repo, subject, bot=bot) if subject else None
        )
        return bound.stamp({"identity": {"login": viewer, "subject": subject, "canWrite": can_write}})


# The verbs that leave a mark on the forge, named here so the HTTP layer can
# refuse an unmanaged repository before one of them runs. The classification
# lives beside the route table because that is where a new verb gets added, and
# a verb added to one and not the other is the mistake this placement is meant
# to make loud.
#
# The read verbs are absent from this set on purpose, and what that buys today
# is narrower than it reads. This set is the HTTP layer's gate, and it covers
# writes: a write against an unmanaged repository is refused here, before a
# verb runs. The reads are not gated *here* -- but every verb that spends the
# credential asks for it through `BrokeredCredential.ensure`, and on the one
# forge this install ships the credential is minted per managed repository, so
# the refresh refuses an unmanaged one with the same 403 before any call is
# made. In practice, then, `capabilities` is the only verb an unmanaged
# repository can be asked, and `clone`, `proposal-list/view/commits`,
# `issue-list/view`, `identity` and `branch-view` refuse it too. The intent that reads of a repository this
# install does not write to should work -- a public upstream, read with no
# credential at all -- is real and is not implemented by this set; it needs a
# credential-less read path, which the design lists as open. Until then the
# managed list is, in effect, a visibility control as well as a write one on
# that forge, and the test beside this classification says so.
WRITE_VERBS = frozenset(
    {
        "publish",
        "proposal-create",
        "proposal-comment",
        "proposal-update",
        "proposal-close",
        "proposal-acknowledge",
        "issue-create",
        "issue-comment",
        "issue-update",
        "issue-close",
        "label-ensure",
        "branch-delete",
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
        "proposal-update": broker.proposal_update,
        "proposal-close": broker.proposal_close,
        "proposal-commits": broker.proposal_commits,
        "proposal-acknowledge": broker.proposal_acknowledge,
        "issue-update": broker.issue_update,
        "issue-close": broker.issue_close,
        "label-ensure": broker.label_ensure,
        "identity": broker.identity,
        "branch-view": broker.branch_view,
        "branch-delete": broker.branch_delete,
    }


__all__ = [
    "Binding",
    "VcsBroker",
    "AGENT_BRANCH_PREFIX",
    "WRITE_VERBS",
    "max_bundle_bytes",
    "route_table",
]
