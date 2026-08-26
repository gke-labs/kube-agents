"""Broker-owned git working trees the agent never gets a path to.

Why this exists
---------------
Today the agent authors files on a shared volume and the broker runs `git` in
that same directory, at a working directory the agent chooses and reports. Every
config-driven code-execution path found against the broker follows from that one
arrangement: a repo-local `.git/config` the agent can write is read by a `git`
holding the cloud credentials, and `.git/hooks/pre-commit` needs no unusual argv
at all — an ordinary `git commit` runs it.

That surface cannot be closed by enumeration. `filter.<name>.smudge` and
`alias.<name>` take an arbitrary name *inside* the config key, so there is no
finite set of keys to deny, and `url.<host>.insteadOf` survives every transport
control because the attacker's URL is `https` too.

So the boundary moves. The agent stops handing over a directory and starts
handing over content: `{path, bytes}` pairs plus a branch and a message. The
broker owns the tree, on its own volume, and the agent has no name for it. There
is no `.git` for the agent to write into because there is no path it can reach.

What is a control here and what is only geometry
------------------------------------------------
Two different things hold this together and they are not equally strong.

*Structural, and checked:* `assert_disjoint_roots` refuses to arm the feature if
the tree root and the agent-shared workspace root overlap. That is an executed
property rather than a comment, which is the point — a future edit that points
both at the same volume fails at startup instead of silently reopening
everything above.

*Geometry, and not checked here:* in the sidecar deployment the tree root lives
on the broker's own emptyDir, which the agent container does not mount. Nothing
in this process can verify that. It is the same argument the credential proxy
already makes twice — about `$HOME/.gitconfig` and about `KUBECTL_KUBERC` — and
it is exactly as weak here as it is there. The operator can defeat it in one
line of a PlatformAgent CR, because `spec.deployment.extraVolumeMounts` is
appended to the agent container unvalidated. The genuinely structural version of
this is the pod split, where there is no shared mount to name.

Paths are validated once, in `repo_relative`, and the same validator runs on
reads and on writes. One parser, both directions: a checker that disagrees with
itself about what `manifests/../.git/config` means is the defect class that has
produced every Critical in this project so far.

Two things that are easy to state and were not true until they were enforced.
No response carries an absolute filesystem path — including the error
responses, which is where one leaks, because git quotes paths in its own
messages and nobody writes an error thinking about it. `_redact` is what makes
that a property rather than an intention, and it scrubs every absolute path
rather than the two this module happens to know the names of. And every verb takes a lock for its
whole duration, because the handler is threaded and each verb is a
read-then-act on a tree another verb may delete or reset underneath it.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

LOGGER = logging.getLogger("credential-proxy")

# Per-file and per-request ceilings. Deliberately small: this carries Kubernetes
# manifests, not build artefacts. Every one of them is checked *before* a single
# byte is written, so a request that exceeds any limit leaves the tree exactly as
# it found it rather than half-applied.
DEFAULT_MAX_FILE_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 8 * 1024 * 1024

# Ceilings on the trees themselves rather than on one request. A clone is the
# only operation here whose size the caller does not state up front, and the
# trees live on the broker's own emptyDir, which is node ephemeral storage --
# so an unbounded one is node disk pressure and eventually an evicted Pod.
# `CREDENTIAL_PROXY_MAX_CLONE_BYTES` and its 256 MiB default are named to match
# the equivalent control in #913, so that whichever implementation survives, the
# operator-facing knob does not change under anyone.
DEFAULT_MAX_CLONE_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_WORKSPACES = 8
DEFAULT_MAX_ENTRIES = 256

# Branch names the product refuses to author, mirroring
# `submit_suggestion.PROTECTED_BRANCHES`. Named here as well rather than
# imported: this is the enforcement point, and a control that depends on a skill
# module being importable is a control that disappears when the skill moves.
PROTECTED_BRANCHES = frozenset({"main", "master", "production"})

# The complete set of git subcommands this module ever issues. Not a policy
# knob and not derived from any request — a literal, so that "what git can the
# broker run on its own behalf" is answerable by reading one line. Enforced in
# the executor as well; this copy is what makes the intent reviewable.
WORKSPACE_GIT_SUBCOMMANDS = frozenset(
    {
        "clone",
        "fetch",
        "checkout",
        "add",
        "commit",
        "push",
        "rev-parse",
        "diff",
        "clean",
        "check-ref-format",
        "symbolic-ref",
    }
)

_HANDLE_RE = re.compile(r"\A[0-9a-f]{32}\Z")
# The same grammar unanchored, for taking handles back out of git's stderr.
_ANY_HANDLE_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
# Any remaining absolute path in a message bound for the wire. The lookbehind
# keeps it off the tail of a ref -- `origin/main` and `refs/heads/x` are not
# paths and must survive, since they are most of what a git error is about.
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w/])/(?:[\w.@+-]+/)*[\w.@+-]+")
_GIT_SHORTNAME_RE = re.compile(r"\Agit~[0-9]+\Z", re.IGNORECASE)


class ContentWorkspaceError(Exception):
    """A request that cannot be served, with the HTTP status it deserves."""

    status = 400
    code = "workspace.invalid"


class PathRefused(ContentWorkspaceError):
    status = 403
    code = "workspace.path.refused"


class TooLarge(ContentWorkspaceError):
    status = 413
    code = "workspace.too-large"


class NoSuchHandle(ContentWorkspaceError):
    status = 404
    code = "workspace.unknown-handle"


class Conflict(ContentWorkspaceError):
    status = 409
    code = "workspace.conflict"


class GitFailed(ContentWorkspaceError):
    status = 502
    code = "workspace.git-failed"


def _limit(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    # A zero or negative ceiling would read as "no limit" to a naive comparison.
    # It means "misconfigured", and the safe reading of misconfigured is the
    # default rather than unbounded.
    return value if value > 0 else default


def max_file_bytes() -> int:
    return _limit("CREDENTIAL_PROXY_WORKSPACE_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES)


def max_total_bytes() -> int:
    return _limit("CREDENTIAL_PROXY_WORKSPACE_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES)


def max_entries() -> int:
    return _limit("CREDENTIAL_PROXY_WORKSPACE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)


def max_clone_bytes() -> int:
    return _limit("CREDENTIAL_PROXY_MAX_CLONE_BYTES", DEFAULT_MAX_CLONE_BYTES)


def max_workspaces() -> int:
    return _limit("CREDENTIAL_PROXY_MAX_WORKSPACES", DEFAULT_MAX_WORKSPACES)


def _tree_bytes(path: Path) -> int:
    """What the clone actually cost on disk, `.git` included.

    Measured after the fact rather than predicted: git offers no honest way to
    ask a remote how big a checkout will be, and `--depth`/`--filter` reduce the
    common case without bounding the worst one. So the clone runs under the
    executor's own timeout, and this decides whether it may stay.
    """
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            # A file that vanished under the walk contributes nothing. Racing
            # the walk cannot make the answer smaller than the truth in a way
            # that matters: the next verb measures again.
            continue
    return total


def _looks_like_dot_git(component: str) -> bool:
    """Every spelling git itself treats as `.git`, and a few it does not.

    git refuses `.git` in a tree under several equivalences, because the
    filesystems it runs on disagree about what a name is: NTFS strips trailing
    dots and spaces and offers the 8.3 shortname `GIT~1`, HFS+ ignores a set of
    zero-width codepoints inside a name. `is_ntfs_dotgit` and `is_hfs_dotgit` in
    git's own source are the reference.

    This is deliberately *stricter* than git. Refusing a manifest that happens
    to be called `.git.` costs nothing — no such file exists in a GitOps
    repository — while matching git's rule exactly would mean reimplementing two
    of its parsers and betting they agree with the version in the image. That
    bet is the one this project keeps losing.
    """
    stripped = "".join(
        character
        for character in component
        # The HFS+ ignorable set git skips: zero-width and directionality marks.
        if unicodedata.category(character) != "Cf"
    )
    stripped = stripped.rstrip(". ").casefold()
    return stripped == ".git" or bool(_GIT_SHORTNAME_RE.match(stripped))


def repo_relative(path: str) -> PurePosixPath:
    """The one path validator, used by reads and writes alike.

    Refuses rather than normalises. `a//b` and `a/./b` are names git would
    accept and a reviewer would read as `a/b`; rather than deciding which of us
    is right, neither is allowed through. A checker that normalises is
    reimplementing another parser's edge cases and betting on the agreement,
    and that bet is the one defect class this codebase keeps producing.
    """
    if not isinstance(path, str) or not path:
        raise PathRefused("path must be a non-empty string")
    if "\x00" in path:
        raise PathRefused("path contains a NUL byte")
    if "\\" in path:
        # Windows separators are a component character on Linux and a separator
        # to git's NTFS-aware checks. Two readings, so: neither.
        raise PathRefused("path contains a backslash")
    if path.startswith("/"):
        raise PathRefused("path must be relative to the repository root")
    components = path.split("/")
    for component in components:
        if component in ("", ".", ".."):
            raise PathRefused(
                f"path component {component!r} is not allowed; give a plain "
                "relative path with no empty, '.' or '..' components"
            )
        if _looks_like_dot_git(component):
            raise PathRefused(
                "paths inside the git metadata directory are refused: the "
                "repository's own configuration and hooks are not content"
            )
    return PurePosixPath(*components)


def _no_symlink_on_the_way(root: Path, relative: PurePosixPath) -> Path:
    """Resolve `relative` under `root`, refusing every symlink in the way.

    `repo_relative` settles what the *name* means; this settles what the
    filesystem will do with it. A repository can legitimately contain a symlink
    — `manifests/vendor -> ../vendor` is ordinary — and writing through one
    lands the file somewhere the name did not say. Checking the final resolved
    path against the root is not enough on its own either, because the resolved
    target can be inside the root and still not be the file the caller named.

    So: no symlink at any depth, including the leaf. A repository that needs one
    on a path the agent writes is a repository this mechanism refuses, loudly,
    rather than one it silently follows.
    """
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise PathRefused(
                f"{'/'.join(relative.parts)} passes through a symbolic link "
                f"({component}); the broker will not write through one"
            )
    # Belt and braces: even with no symlink found, prove the composed path is
    # under the root before anything opens it.
    resolved = current.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathRefused("path escapes the repository root")
    return current


def assert_disjoint_roots(tree_root: Path, agent_workspace_root: Path) -> None:
    """Refuse to arm content-passing if the agent can name the tree.

    The whole mechanism is "the broker operates somewhere the agent has no path
    to". If the two roots overlap, that sentence is false and every finding
    content-passing exists to close is open again — with the added insult that
    the code claims otherwise.

    Executed at construction, so a mount rearrangement that points both at the
    same volume is a broker that will not start, not a broker that starts
    without the property. A property the deployment is supposed to have is
    worth a call rather than a comment.
    """
    tree = Path(tree_root).resolve()
    agent = Path(agent_workspace_root).resolve()
    if tree == agent or agent in tree.parents or tree in agent.parents:
        raise RuntimeError(
            f"the content workspace root ({tree}) and the agent-shared "
            f"workspace root ({agent}) overlap. Content-passing exists to keep "
            "the broker out of directories the agent can write; with these "
            "roots it would not. Refusing to start."
        )


@dataclass
class Change:
    """One entry of a commit payload: bytes to write, or a deletion."""

    path: PurePosixPath
    content: bytes | None  # None means delete

    @property
    def deletes(self) -> bool:
        return self.content is None


def parse_changes(raw: object) -> list[Change]:
    """Validate a whole payload before any of it is applied.

    Every ceiling is checked here, ahead of the first write, so an oversized
    request leaves the tree untouched instead of half-applied. Fail closed means
    the failure happens before the side effects, not after some of them.
    """
    if not isinstance(raw, list) or not raw:
        raise ContentWorkspaceError("changes must be a non-empty list")
    if len(raw) > max_entries():
        raise TooLarge(
            f"{len(raw)} changes exceeds the {max_entries()} allowed in one commit"
        )

    changes: list[Change] = []
    seen: set[str] = set()
    total = 0
    for entry in raw:
        if not isinstance(entry, dict):
            raise ContentWorkspaceError("each change must be an object")
        relative = repo_relative(entry.get("path"))
        key = str(relative)
        if key in seen:
            # Two entries for one path have no defined winner, and picking one
            # silently is how a reviewer and the broker end up disagreeing about
            # what was committed.
            raise ContentWorkspaceError(f"{key} appears twice in one commit")
        seen.add(key)

        if entry.get("delete") is True:
            if "contentBase64" in entry:
                raise ContentWorkspaceError(
                    f"{key}: a change is either a deletion or content, not both"
                )
            changes.append(Change(relative, None))
            continue

        encoded = entry.get("contentBase64")
        if not isinstance(encoded, str):
            # Base64 only, never a `content` string alongside it. One encoding
            # means there is no question about which one a byte came through,
            # and it carries binary without a second code path.
            raise ContentWorkspaceError(
                f"{key}: contentBase64 is required (base64 of the file's bytes); "
                "there is no plaintext form"
            )
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ContentWorkspaceError(f"{key}: contentBase64 is not valid base64") from exc
        if len(content) > max_file_bytes():
            raise TooLarge(
                f"{key} is {len(content)} bytes, over the {max_file_bytes()}-byte limit"
            )
        total += len(content)
        if total > max_total_bytes():
            raise TooLarge(
                f"the commit totals more than the {max_total_bytes()}-byte limit"
            )
        changes.append(Change(relative, content))
    return changes


def check_branch_name(name: object) -> str:
    """A string the broker is willing to put in a branch position at all.

    Separate from `check_branch` because reading a branch and authoring one are
    different permissions. Both share this half.

    A leading `-` is refused *first*, before anything else looks at the string.
    Everything downstream — including git's own `check-ref-format --branch` —
    reads a leading dash as an option, so a name like `--upload-pack=id` would
    be validated as a flag and then reappear as one in a later argv. The check
    that has to run before the parsers is the one about whether this is an
    argument at all.

    Beyond that the shape is git's problem, not this module's:
    `check-ref-format` is delegated to by the caller rather than reimplemented.
    A regex here would be a guess about what git accepts, and guesses fail
    permissively.
    """
    if not isinstance(name, str) or not name.strip():
        raise ContentWorkspaceError("branch must be a non-empty string")
    branch = name.strip()
    if branch.startswith("-"):
        raise ContentWorkspaceError(
            "branch must not start with '-': every parser downstream would read "
            "it as an option"
        )
    return branch


def check_branch(name: object) -> str:
    """A branch name the broker is willing to *author*.

    `check_branch_name` first, then the protected set, so a suggestion can never
    target the rollout branch. Only the write path uses this. Basing a
    workspace on `main` is the ordinary case and goes through
    `check_branch_name` alone — refusing to read the branch everything is cut
    from would make the feature useless, and reading is not authoring.
    """
    branch = check_branch_name(name)
    if branch.casefold() in PROTECTED_BRANCHES:
        raise ContentWorkspaceError(
            f"'{branch}' is a rollout branch; suggestions are proposed on their "
            "own branch and merged by a human"
        )
    return branch


@dataclass
class Workspace:
    """One broker-owned clone, named by an unguessable handle."""

    handle: str
    repo: str
    tree: Path
    base: str
    base_sha: str
    branch: str | None = None
    head: str | None = None
    metadata: dict = field(default_factory=dict)


GitRunner = Callable[..., object]


class ContentWorkspaceStore:
    """Broker-side ownership of the trees. Nothing here takes a caller path.

    The handle is a bearer capability, minted here and never derived from
    anything the caller sent. Be precise about what that buys: it stops one
    session naming another's tree by guessing, and it removes the `.lease` file
    that any agent could `touch` to unlock every mutating verb. It is **not** an
    ownership check, because the broker still cannot tell two sessions in the
    agent container apart: everything on the socket arrives with the same
    identity. A session that can read another's output has its handle. That is
    a real improvement over a file anyone can create and it is not
    authorization; per-caller identity is what would make it one, and that does
    not exist yet.
    """

    def __init__(
        self,
        tree_root: str | Path,
        agent_workspace_root: str | Path,
        runner: GitRunner,
    ) -> None:
        # Resolved, because `assert_disjoint_roots` resolves both sides and
        # `_redact` matches this value against paths git prints -- which git
        # has already resolved. Unresolved, a symlinked prefix makes the
        # redaction match only the tail (`/private<workspace-root>/...` on a
        # Mac), and the module's invariant would depend on whichever caller
        # happened to resolve the root before handing it over.
        self.tree_root = Path(tree_root).resolve()
        assert_disjoint_roots(self.tree_root, Path(agent_workspace_root))
        self.tree_root.mkdir(parents=True, exist_ok=True)
        # 0o700 rather than the default: the state directory is the broker's
        # own, but the mode is free and it means a second process running as
        # another uid in this container gains nothing from finding the path.
        try:
            self.tree_root.chmod(0o700)
        except OSError:
            # Logged rather than swallowed. The mode is defence in depth here --
            # the state dir is not mounted in the agent container at all -- but
            # a control that fails silently is one nobody finds out about, and
            # the `git_hooks_dir` chmod in the executor already warns.
            LOGGER.warning("could not restrict the content workspace root %s", self.tree_root)
        self._runner = runner
        self._workspaces: dict[str, Workspace] = {}
        # One lock, held across the whole of every public verb.
        #
        # The handler runs on a `ThreadingHTTPServer`, so two requests naming
        # one handle genuinely interleave, and every verb here is a
        # read-then-act on a working tree that the other verb is entitled to
        # delete or reset underneath it. Two interleavings were reproduced
        # before this went in. `close` between a commit's `checkout` and its
        # write loop leaves the commit re-creating the tree it just removed,
        # with no handle registered against it -- the same orphan class the
        # failed-clone fix closes, on a path that fix does not cover. And a
        # second `commit` inside the first has its `clean -fdxq` delete the
        # first's files, so the first either fails on a pathspec that no longer
        # matches or lands its commit on the other's branch while reporting its
        # own.
        #
        # Reentrant because the verbs call `get`, which takes it too.
        #
        # Coarse on purpose, and the cost is real rather than theoretical: this
        # is held across `clone`, `fetch` and `push`, so it is held across
        # network I/O bounded only by the executor's timeout, not by anything
        # here. Measured: a `get` on an *unrelated* workspace blocked 2.7s
        # behind a 3s clone. The store therefore serves one request at a time,
        # worst case for as long as the timeout allows, and that includes
        # `close` -- the verb an operator reaches for when they want the tree
        # gone. Note that `max_workspaces` advertises a concurrency this
        # forbids: eight may be open, one may be doing anything.
        #
        # Still the right trade for a single agent Pod publishing one pull
        # request at a time, and a per-workspace lock would still need this one
        # to guard the dict it lives in. Worth revisiting the day a caller has
        # a reason to run two workspaces at once, which nothing does today.
        self._lock = threading.RLock()

    # -- git -------------------------------------------------------------

    def _redact(self, text: object) -> str:
        """git's stderr, with every absolute path taken out of it.

        Every docstring in this module says no response carries a filesystem
        path, and until this existed that was an assertion with nothing
        enforcing it: git's own errors quote absolute paths freely (`fatal:
        could not create work tree dir '<root>/<handle>/repo'`), and `_git` puts
        500 bytes of stderr on the wire. An error message is exactly where a
        path leaks, because nobody writes an error message thinking about it.

        Three passes, widening. The tree root and the handle are the two that
        matter, and they get their own markers so the message still reads. The
        third pass takes out *any* remaining absolute path, and it is the one
        that makes the claim true rather than nearly true: a first version of
        this knew only the root and the handle grammar, and git has other paths
        to talk about. `warning: unable to access '<broker-home>/.gitconfig'`
        is one the broker really can emit, because `$HOME` is deliberately the
        broker's own state dir; `error: could not lock config file <tmp>/...`
        is another. Neither is agent-writable and neither is worth much to an
        attacker, but a claim asserted in four places should not have a list of
        exceptions -- the leak nobody predicted is the whole failure mode here.

        The cost is that an error naming a path says `<path>` instead. The verb
        and the reason survive, which is what a caller can act on anyway; the
        unredacted text goes to the broker's log, where the operator can read
        it.
        """
        rendered = (text or "").strip() if isinstance(text, str) else ""
        # The root *and everything under it*, in one substitution. Replacing
        # only the root leaves `<workspace-root>/<handle>/repo`, and the third
        # pass then chews the tail into a second marker -- a message that is
        # correctly redacted and unreadable. One marker for one path.
        rendered = re.sub(
            re.escape(str(self.tree_root)) + r"(?:/[\w.@+-]+)*",
            "<workspace-root>",
            rendered,
        )
        # By shape, not by lookup. The handle that most needs removing is the
        # one whose `open` just failed, and that one is not in `_workspaces`
        # yet -- registration is the last thing `open` does. Matching the
        # handle grammar catches it and every registered one with one rule,
        # including one quoted on its own rather than inside a path.
        rendered = _ANY_HANDLE_RE.sub("<handle>", rendered)
        rendered = _ABSOLUTE_PATH_RE.sub("<path>", rendered)
        return rendered[:500]

    def _git(self, workspace_or_dir, argv: list[str], *, check: bool = True):
        cwd = (
            workspace_or_dir.tree
            if isinstance(workspace_or_dir, Workspace)
            else Path(workspace_or_dir)
        )
        result = self._runner(["git", *argv], cwd=cwd)
        exit_code = getattr(result, "exit_code", 1)
        if check and exit_code != 0:
            raise GitFailed(
                f"`git {argv[0]}` failed with exit code {exit_code}: "
                f"{self._redact(getattr(result, 'stderr', ''))}"
            )
        return result

    @staticmethod
    def _out(result) -> str:
        return (getattr(result, "stdout", "") or "").strip()

    # -- lifecycle -------------------------------------------------------

    def open(self, repo: str, base: str | None = None) -> Workspace:
        """Clone `repo` into a fresh tree and return its handle."""
        if not isinstance(repo, str) or not is_owner_name(repo):
            raise ContentWorkspaceError("repo must be owner/name")
        # `base` names a branch exactly as the commit payload's does, so it goes
        # through the same check. Not reachable as an option today -- every use
        # of it is prefixed with `origin/` before it reaches an argv -- but a
        # reader comparing the two would reasonably assume the validation is
        # symmetric, and one day a caller will thread it somewhere unprefixed.
        if base is not None:
            base = check_branch_name(base)
        with self._lock:
            if len(self._workspaces) >= max_workspaces():
                raise TooLarge(
                    f"{len(self._workspaces)} workspaces are already open, which is "
                    f"the limit of {max_workspaces()}; close one before opening another"
                )
            handle = os.urandom(16).hex()
            tree = self.tree_root / handle
            tree.mkdir(parents=True, exist_ok=False)
            url = f"https://github.com/{repo}.git"
            # The URL is composed here from a validated `owner/name`, never taken
            # from the caller: a caller-supplied URL is `url.<host>.insteadOf` by
            # another route, and the whole point of this module is that the agent
            # does not choose where the credentials go.
            # Everything from here to the registration is undone on failure. The
            # directory is created before the clone that fills it, so a clone that
            # cannot authenticate leaves a tree with no handle pointing at it —
            # unreachable by `close` and therefore permanent for the life of the
            # container. Observed against a real install: a private repository with
            # no credential available left one directory per attempt.
            try:
                self._git(tree, ["clone", "--quiet", url, str(tree / "repo")])
                # Measured after the clone, and the tree goes if it is over.
                # A repository the broker cannot afford to hold is not one it
                # should hold *badly*, half-cloned and still on the disk.
                size = _tree_bytes(tree)
                if size > max_clone_bytes():
                    raise TooLarge(
                        f"{repo} is {size} bytes cloned, over the "
                        f"{max_clone_bytes()}-byte limit; the tree was removed"
                    )
                workspace = Workspace(
                    handle=handle,
                    repo=repo,
                    tree=tree / "repo",
                    base="",
                    base_sha="",
                )
                workspace.base = base or self._default_branch(workspace)
                workspace.base_sha = self._sha(workspace, f"origin/{workspace.base}")
            except BaseException:
                _remove_tree(tree)
                raise
            self._workspaces[handle] = workspace
            return workspace

    def _default_branch(self, workspace: Workspace) -> str:
        result = self._git(
            workspace,
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            check=False,
        )
        ref = self._out(result)
        if getattr(result, "exit_code", 1) != 0 or not ref:
            return "main"
        return ref.split("/", 1)[1] if ref.startswith("origin/") else ref

    def _sha(self, workspace: Workspace, ref: str) -> str:
        return self._out(self._git(workspace, ["rev-parse", "--verify", ref]))

    def get(self, handle: object) -> Workspace:
        if not isinstance(handle, str) or not _HANDLE_RE.match(handle):
            raise NoSuchHandle("handle is not a workspace handle")
        with self._lock:
            workspace = self._workspaces.get(handle)
        if workspace is None:
            raise NoSuchHandle("no such workspace; open one first")
        return workspace

    def close(self, handle: str) -> None:
        with self._lock:
            workspace = self.get(handle)
            self._workspaces.pop(workspace.handle, None)
            _remove_tree(workspace.tree.parent)

    # -- reads -----------------------------------------------------------

    def read(self, handle: str, path: str) -> bytes:
        """The content of one tracked file. A read returns bytes, never a path."""
        with self._lock:
            workspace = self.get(handle)
            relative = repo_relative(path)
            target = _no_symlink_on_the_way(workspace.tree, relative)
            if not target.is_file():
                raise ContentWorkspaceError(
                    f"{relative} is not a file in this repository"
                )
            size = target.stat().st_size
            if size > max_file_bytes():
                raise TooLarge(
                    f"{relative} is {size} bytes, over the {max_file_bytes()}-byte "
                    "read limit"
                )
            return target.read_bytes()

    def list(self, handle: str, prefix: str | None = None) -> list[dict]:
        """Tracked paths and their sizes. Names, not handles to the filesystem.

        `prefix` names a directory, and it is matched component-wise. A plain
        string comparison would make `prefix="a"` return `ab/x` as well as
        `a/y`, which reads as a listing bug rather than as a filter and would
        have a caller paging through files it did not ask for.
        """
        with self._lock:
            workspace = self.get(handle)
            under = repo_relative(prefix).parts if prefix else ()
            entries: list[dict] = []
            for path in sorted(workspace.tree.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(workspace.tree)
                parts = relative.parts
                if any(_looks_like_dot_git(part) for part in parts):
                    continue
                if under and parts[: len(under)] != under:
                    continue
                entries.append(
                    {"path": str(PurePosixPath(*parts)), "size": path.stat().st_size}
                )
            return entries

    # -- writes ----------------------------------------------------------

    def commit(
        self,
        handle: str,
        branch: str,
        message: str,
        changes: Iterable[Change],
        expected_base_sha: str | None = None,
    ) -> dict:
        """Apply the payload on a fresh branch off the base, and commit it."""
        with self._lock:
            workspace = self.get(handle)
            branch = check_branch(branch)
            self._git(workspace, ["check-ref-format", "--branch", branch])
            if not isinstance(message, str) or not message.strip():
                raise ContentWorkspaceError("message must be a non-empty string")
            changes = list(changes)

            self._git(workspace, ["fetch", "--quiet", "--prune", "origin"])
            current_base_sha = self._sha(workspace, f"origin/{workspace.base}")
            if expected_base_sha and expected_base_sha != current_base_sha:
                self._raise_if_base_moved_under_us(
                    workspace, expected_base_sha, current_base_sha, changes
                )
            workspace.base_sha = current_base_sha

            self._git(
                workspace,
                ["checkout", "--force", "-B", branch, f"origin/{workspace.base}"],
            )
            # The tree is the broker's, so a leftover file from an earlier commit on
            # this handle is debris rather than someone's unsaved work. `-x` as well,
            # because nothing here is ignorable-but-wanted.
            self._git(workspace, ["clean", "-fdxq"], check=False)

            paths: list[str] = []
            for change in changes:
                target = _no_symlink_on_the_way(workspace.tree, change.path)
                if change.deletes:
                    if target.is_file():
                        target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(change.content or b"")
                paths.append(str(change.path))

            # `--literal-pathspecs` so a manifest whose name contains a glob
            # character is one file rather than a pattern, and `--` so a path can
            # never be read as an option.
            self._git(workspace, ["--literal-pathspecs", "add", "--", *paths])

            staged = self._git(workspace, ["diff", "--cached", "--quiet"], check=False)
            code = getattr(staged, "exit_code", 1)
            if code == 0:
                return {"committed": False, "branch": branch, "base": workspace.base}
            if code != 1:
                # Anything other than 0 or 1 means the index could not be read, and
                # "no difference" is then a guess. `audit_report` learned this the
                # expensive way: it read every non-zero exit as "already fixed".
                raise GitFailed(
                    f"`git diff --cached --quiet` exited {code}; the index could not "
                    "be read, so it is not safe to say whether there is anything to commit"
                )

            self._git(workspace, ["commit", "-m", message])
            workspace.branch = branch
            workspace.head = self._sha(workspace, "HEAD")
            return {
                "committed": True,
                "branch": branch,
                "base": workspace.base,
                "baseSha": workspace.base_sha,
                "commit": workspace.head,
            }

    def _raise_if_base_moved_under_us(
        self,
        workspace: Workspace,
        expected: str,
        current: str,
        changes: list[Change],
    ) -> None:
        """A moved base is only a conflict if it touched a path we are writing.

        Refusing every commit whose base advanced would mean a ten-minute audit
        fails behind any unrelated merge, which is most of them. Refusing only
        when the same file moved is the answer a human reviewer would give.
        """
        touched = [str(change.path) for change in changes]
        diff = self._git(
            workspace,
            ["--literal-pathspecs", "diff", "--name-only", expected, current, "--", *touched],
            check=False,
        )
        if getattr(diff, "exit_code", 1) != 0:
            # Could not compare — most likely the expected sha is no longer
            # reachable after a force-push to the base. That is a conflict by
            # any reading, and guessing otherwise would overwrite whatever
            # happened.
            raise Conflict(
                f"the base branch moved from {expected} to {current} and the two "
                "could not be compared; re-read the files and submit again",
            )
        collided = [line for line in self._out(diff).splitlines() if line.strip()]
        if collided:
            raise Conflict(
                f"the base branch moved from {expected} to {current} and it "
                f"changed {len(collided)} file(s) this commit also writes: "
                f"{', '.join(collided[:10])}. Re-read them and submit again."
            )

    def push(self, handle: str, branch: str) -> dict:
        """Push the committed branch, without the right to destroy another.

        `--force-with-lease` and deliberately no fetch immediately before it:
        fetching first is the classic way to defeat the lease, because it moves
        the remote-tracking ref onto whatever landed in the meantime and the
        lease then compares that value against itself.
        """
        with self._lock:
            workspace = self.get(handle)
            branch = check_branch(branch)
            if workspace.branch != branch:
                raise ContentWorkspaceError(
                    f"nothing has been committed on '{branch}' in this workspace"
                )
            result = self._git(
                workspace,
                ["push", "--force-with-lease", "origin", branch],
                check=False,
            )
            if getattr(result, "exit_code", 1) != 0:
                stderr = (getattr(result, "stderr", "") or "").lower()
                if "stale info" in stderr or "rejected" in stderr:
                    raise Conflict(
                        f"the remote '{branch}' moved since this workspace fetched "
                        "it; the push was refused rather than overwriting it"
                    )
                raise GitFailed(
                    f"push failed: {self._redact(getattr(result, 'stderr', ''))}"
                )
            return {"pushed": True, "branch": branch, "commit": workspace.head}


def is_owner_name(value: str) -> bool:
    """`owner/name`, with nothing in it that could be read as anything else."""
    parts = value.split("/")
    if len(parts) != 2:
        return False
    return all(re.fullmatch(r"[A-Za-z0-9._-]{1,100}", part) and part not in (".", "..") for part in parts)


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
