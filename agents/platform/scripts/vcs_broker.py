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

The routes are stateless to their callers. There is no handle, no answer depends
on an earlier request, and two concurrent requests share nothing but the lock the
HTTP layer holds and the proposal-outcome counts' own, which guards two metrics and
never an answer.

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
import datetime
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from providers import (
    CliTransport,
    HttpTransport,
    BrokeredCredential,
    Forge,
    ForgeUnsupported,
    IdentityUnknown,
    MAX_PAGE_SIZE,
    NoCredential,
    Registry,
    Transport,
    pinned_base,
    short_branch,
    tls_refusal as tls_refusal_default,
    validate_branch,
    validate_number,
    validate_revision,
)
from providers.credentials import CREDENTIAL_HELPER_KEY
from workspace_paths import WorkspaceError, validate_path

LOGGER = logging.getLogger("credential-proxy.vcs")
#: The key in a forge's credentials Secret that holds its read-only token.
FORGE_READ_TOKEN_KEY = "read-token"

# The same shape of ceiling `content_workspace` applies, for the same reason:
# the broker's scratch volume is an emptyDir sized for manifests, and a
# repository that does not fit should say so rather than fill the disk out from
# under everything else.
DEFAULT_MAX_CLONE_BYTES = 256 << 20  # 256 MiB
DEFAULT_MAX_BUNDLE_BYTES = 64 << 20  # 64 MiB
# The `snapshot` tar's ceiling, and the variable that overrides it. Below the
# bundle's: the answer is base64 inside one JSON body, and each of the copies
# a reply holds (the file, the base64 text, the JSON text, its encoding) is
# about 4/3 of this. The memory a snapshot takes is not measured yet.
DEFAULT_MAX_SNAPSHOT_BYTES = 32 << 20  # 32 MiB
MAX_SNAPSHOT_BYTES_ENV = "CREDENTIAL_PROXY_MAX_SNAPSHOT_BYTES"
# The in-process API client's bounds when nothing hands it the executor's: the
# same order as a forge CLI call is given, and a ceiling on one answer that a
# page of JSON never approaches.
DEFAULT_HTTP_TIMEOUT_SECONDS = 60.0
DEFAULT_HTTP_MAX_BYTES = 8 << 20  # 8 MiB

# How many open proposals the `advance` check reads off a branch. One would
# settle whether any is open; the rest are read because the second half of the
# check asks whose they are, and a forge that lets two proposals share a source
# branch would otherwise have the answer decided by whichever came back first.
# Not a page to walk: a branch with more than this many open proposals on it is
# not a case this refusal is trying to be exact about.
OPEN_PROPOSALS_ON_A_BRANCH = 10

# The spellings `_short_ref` reads as the bare branch. Wider than
# `providers.validate.BRANCH_REF_PREFIXES` on purpose: it feeds the
# protected-branch check, where reading `heads/main` as `main` refuses more,
# and the branch verbs. A proposal's target is compared through `short_branch`
# instead, because a forge opens the proposal on the name it was given.
_SHORT_REF_PREFIXES = ("refs/heads/", "heads/")

# The ref an incoming bundle is fetched into. Under `refs/vcs/` rather than
# `refs/heads/` so nothing here can be confused with a branch, and so a publish
# of a leftover ref cannot happen by naming a plausible branch.
_INCOMING = "refs/vcs/incoming"

# What `git ls-remote --exit-code` exits with when the remote answered and holds
# no matching ref; any other failure is a remote it could not ask.
LS_REMOTE_NO_MATCH_EXIT_CODE = 2

# The namespace every branch this install publishes is named under, and the only
# one `branch-delete` will touch. Every shipped caller derives its branch as
# `platform-agent/<change>-<target>`; a branch outside it is a person's, or
# another tool's, whatever its history says.
AGENT_BRANCH_PREFIX = "platform-agent/"
# The neutral proposal state a forge reports once a proposal has merged
# (`providers/*/translate.py` maps every forge's spelling to it), and the
# spelling of UTC `datetime.fromisoformat` did not accept before Python 3.11.
MERGED_PROPOSAL_STATE = "merged"
# The neutral state for a proposal closed without a merge, the same adapters'
# spelling; the broker counts it apart from a merge.
CLOSED_PROPOSAL_STATE = "closed"
# A forge answer below this status is the forge's own refusal (401, 403, 404,
# 409, 422, 429: providers/errors.py keeps those as they are), so the write was
# not applied; at or above it the answer was lost or unreadable and the write
# may have been (502, 503 there), which is what the own-close mark is for.
FORGE_REFUSAL_STATUS_CEILING = 500
ISO_UTC_SUFFIX = "Z"
ISO_UTC_OFFSET = "+00:00"

# How many of a branch's proposals `branch-delete` reads. It needs all of them:
# the one that carried the tip, and every other, since a branch any of them
# shows a person proposed from is not this install's to delete. A history the
# forge answers as truncated is therefore refused rather than judged on its
# first page. A spent agent branch carries one or two, but a flow with a fixed
# name -- one per workload, reused on every alert -- adds one per round, so the
# page is the largest a forge serves in one call, and it is still one call.
PROPOSAL_HISTORY_ON_A_BRANCH = MAX_PAGE_SIZE

# A repository's role, as the broker's ConfigMap lists it: `managed` is in
# `managed_repos`, whatever else it is in; `context` is in `context_repos`
# alone; `unregistered` is in neither, or the lists could not be read. Only
# `snapshot` reads it here, to choose the credential its clone presents.
ROLE_MANAGED = "managed"
ROLE_CONTEXT = "context"
ROLE_UNREGISTERED = "unregistered"

# An empty `credential.helper` in the per-invocation config layer clears every
# helper git read before it, the URL-scoped ones of the global file included.
# `snapshot` puts it on every clone that must not ride the ambient write
# credential: a context repository's, and an unregistered one's.
HELPER_CLEARED = (CREDENTIAL_HELPER_KEY, "")

# Where `snapshot` puts its two files inside its scratch directory: the bare
# clone, and the tar `git archive` writes. The tar goes to a file and not to
# stdout, because the git runner cuts stdout at its output ceiling.
SNAPSHOT_CLONE_DIR = "repository.git"
SNAPSHOT_ARCHIVE_FILE = "snapshot.tar"
# What a snapshot answer says about history: a tar carries none.
SNAPSHOT_HISTORY = "none"
# The most `paths` one snapshot request may name. Each is one argv entry.
MAX_SNAPSHOT_PATHS = 256
# How often the snapshot clone's size is measured while it runs.
CLONE_WATCH_SECONDS = 0.5
# A path entry, and all of them joined: well under the kernel's limits on one
# argument (128 KiB) and on a command line, so git always starts.
MAX_SNAPSHOT_PATH_BYTES = 4096
MAX_SNAPSHOT_PATHS_BYTES = 64 << 10
# A full object ID, SHA-1 or SHA-256. `ref` names a branch or a tag:
# `clone --branch` takes no revision, so one is refused by name rather than
# left to fail as a git error.
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}", re.IGNORECASE)
# The ref namespaces a `ref` is looked up in after the clone, in the order
# `clone --branch` itself prefers them.
SNAPSHOT_REF_NAMESPACES = ("refs/heads/", "refs/tags/")
# What `git archive` prints when a path names nothing in the tree.
PATHSPEC_NO_MATCH = "did not match any files"
# What `snapshot` writes into its bare clone's `info/attributes`, the file
# that outranks every `.gitattributes` in the tree, before `git archive` runs.
# Each attribute named here makes `archive` write something other than the
# committed bytes, and some make it far larger: `export-subst` expands every
# `$Format:%B$` to the whole commit message, `ident` grows each `$Id$` about
# twelve times, `working-tree-encoding` can make text four times larger, and
# `text` adds a CR to each line. With them unset the tar holds the files as
# they were committed, and the size measured before the archive holds.
# `export-ignore` is unset too: it leaves files out of the tar with no sign,
# and a caller would read a partial tree as the whole one.
SNAPSHOT_ATTRIBUTES_FILE = Path("info") / "attributes"
SNAPSHOT_ATTRIBUTES = "* -export-subst -export-ignore -ident -text -working-tree-encoding\n"
# What `git ls-tree` prints for each entry of the revision `snapshot` measures:
# the mode, the size (`-` for a tree or a submodule) and the path, NUL-ended.
SNAPSHOT_LISTING_FORMAT = "--format=%(objectmode) %(objectsize) %(path)"
# The tree entry modes the measure reads: a symbolic link's blob is its
# target, which goes in the tar header and not after it, and a tree or a
# submodule is a directory entry whose name ends in `/`.
GIT_MODE_SYMLINK = "120000"
GIT_MODES_DIRECTORY = frozenset({"040000", "160000"})
# The tar `git archive` writes, as the measure counts it: one 512-byte header
# for each entry and the data padded to 512; a pax header (a header block and
# its padded record) for a name or a link target longer than the 100 bytes the
# ustar field holds, with the record's own length, key and newline counted
# generously; the pax header with the commit ID at the start; two zero blocks
# at the end, and the whole padded to a 20-block record.
TAR_BLOCK_BYTES = 512
TAR_NAME_FIELD_BYTES = 100
TAR_PAX_RECORD_OVERHEAD_BYTES = 32
TAR_GLOBAL_HEADER_BYTES = 2 * TAR_BLOCK_BYTES
TAR_END_BYTES = 2 * TAR_BLOCK_BYTES
TAR_RECORD_BYTES = 20 * TAR_BLOCK_BYTES

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


def _base_branch_missing(repo: str, base: str) -> WorkspaceError:
    return WorkspaceError(
        f"{repo} has no branch {base}, the base branch this install is "
        "configured with. Proposals onto this repository can only target that "
        "branch, so it has to exist on the remote first.",
        status=409,
        code="BASE_BRANCH_MISSING",
    )


def _iso_timestamp(value: Any) -> float | None:
    """An ISO-8601 instant as seconds since the epoch, or None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(ISO_UTC_SUFFIX):
        text = text[: -len(ISO_UTC_SUFFIX)] + ISO_UTC_OFFSET
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


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


def _padded(size: int, unit: int) -> int:
    """``size`` rounded up to a whole number of ``unit``."""
    return -(-size // unit) * unit


def _pax_bytes(length: int) -> int:
    """What a pax header for a value of ``length`` bytes adds, or 0 when none is needed."""
    if length <= TAR_NAME_FIELD_BYTES:
        return 0
    return TAR_BLOCK_BYTES + _padded(length + TAR_PAX_RECORD_OVERHEAD_BYTES, TAR_BLOCK_BYTES)


def _tar_bytes(listing: str) -> int:
    """The size of the tar `git archive` writes for ``listing``, a little over and never under.

    ``listing`` is `git ls-tree -r -t -z` in `SNAPSHOT_LISTING_FORMAT`. Every
    path is counted, so a blob named at a thousand paths is counted a thousand
    times, as the tar holds it. An entry `export-ignore` drops is counted too.
    """
    total = TAR_GLOBAL_HEADER_BYTES
    for entry in listing.split("\0"):
        if not entry:
            continue
        mode, size, path = entry.split(" ", 2)
        name_bytes = len(path.encode("utf-8"))
        if mode in GIT_MODES_DIRECTORY:
            name_bytes += 1
        total += TAR_BLOCK_BYTES + _pax_bytes(name_bytes)
        if mode == GIT_MODE_SYMLINK:
            total += _pax_bytes(int(size))
        elif mode not in GIT_MODES_DIRECTORY:
            total += _padded(int(size), TAR_BLOCK_BYTES)
    return _padded(total + TAR_END_BYTES, TAR_RECORD_BYTES)


def holds_stored_token(forge: Forge) -> bool:
    """Whether ``forge`` presents a stored token rather than a minted one.

    A brokered credential is minted per repository and installed for git by
    the CLI, so the broker's own clones of a managed repository ride it
    ambiently. A forge whose credential is neither brokered nor absent has
    nothing ambient behind it, and its clones carry the credential themselves.
    """
    credential = getattr(forge, "credential", None)
    return credential is not None and not isinstance(credential, (BrokeredCredential, NoCredential))


def warn_without_read_token(forge: Forge, credential: Any, where: str) -> None:
    """Say why a context repository's clone presents no credential.

    Only for a forge whose stored token can write: it offers a read-only
    token only through the `read-token` key, and without one the
    clone reads a public repository and a private one refuses it. The log line
    is the operator's only sign of which half is wrong.
    """
    if not isinstance(credential, NoCredential) or not holds_stored_token(forge):
        return
    if not getattr(forge, "read_token_path", ""):
        # The forges file came from an operator older than the read token, so
        # no change to the Secret can help until the operator is upgraded.
        LOGGER.warning(
            "%s role=context: the broker's config names no readTokenPath for the "
            "%s forge (an operator older than the read-only token rendered it), so "
            "the clone presents none; a private repository will refuse it",
            where, forge.name,
        )
        return
    LOGGER.warning(
        "%s role=context: the %s forge has no usable read-only token (key %s in "
        "its credentials Secret: missing, unreadable, empty, or not one line of "
        "printable ASCII) and its stored token can write, so the clone presents "
        "none; a private repository will refuse it",
        where, forge.name, FORGE_READ_TOKEN_KEY,
    )


def _tree_bytes(root: Path, stop_past: int) -> int:
    """The bytes of the files under ``root``, counted until the total passes ``stop_past``."""
    total = 0
    for directory, _subdirs, filenames in os.walk(root):
        for filename in filenames:
            try:
                total += os.lstat(os.path.join(directory, filename)).st_size
            except OSError:
                continue
            if total > stop_past:
                return total
    return total


def _snapshot_paths(raw: Any) -> list[str]:
    """`paths` of a snapshot request: literal repository-relative names, or none.

    Each is checked as the content routes check a name (`validate_path`):
    no absolute path, no `.` or `..` segment, no `.git` segment, no control
    character.
    """
    if raw is None:
        return []
    if not isinstance(raw, list) or not raw:
        raise WorkspaceError("paths must be a non-empty list of repository-relative paths")
    if len(raw) > MAX_SNAPSHOT_PATHS:
        raise WorkspaceError(f"paths names more than {MAX_SNAPSHOT_PATHS} entries")
    paths = [validate_path(path) for path in raw]
    # Checked here, before the clone: the names end up on git's command line,
    # where the kernel refuses an argument or a total past its limits.
    if any(len(path.encode()) > MAX_SNAPSHOT_PATH_BYTES for path in paths):
        raise WorkspaceError(f"a path in `paths` is longer than {MAX_SNAPSHOT_PATH_BYTES} bytes")
    if sum(len(path.encode()) + 1 for path in paths) > MAX_SNAPSHOT_PATHS_BYTES:
        raise WorkspaceError(f"`paths` is longer than {MAX_SNAPSHOT_PATHS_BYTES} bytes in total")
    return paths


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
        return self.refused_drops(lambda: self.transport().api(method, path, **kwargs))

    def whoami(self) -> str:
        """The credential's login, on the credential made current first.

        The forge answers first (`Forge.whoami`): a configured identity, or a
        lookup of its own for a token that cannot call the current-user
        route. None from it means the transport's route, as before. An empty
        login from a forge that declares `identity_required` raises
        `IdentityUnknown`, so that every ownership check refuses instead of
        reading "" as "do not compare".
        """
        self.ensure()
        login = self.forge.whoami(self.api)
        if login is None:
            login = self.refused_drops(lambda: self.transport().whoami())
        if not login and self.forge.identity_required:
            raise IdentityUnknown(self.forge.name)
        return login

    def refused_drops(self, call: Callable[[], Any]) -> Any:
        """Run `call`; on a 401, the credential drops a token it cached, so the next call mints anew."""
        try:
            return call()
        except WorkspaceError as refused:
            if refused.status == 401:
                invalidate = getattr(self.forge.credential, "invalidate", None)
                if invalidate is not None:
                    invalidate(self.repo)
            raise

    def stamp(self, result: dict[str, Any]) -> dict[str, Any]:
        result.update({"forge": self.forge.name, "repo": self.repo})
        return result


class VcsBroker:
    """The verbs, each one request long.

    `scratch_root` is on the broker's own volume. Nothing under it outlives a
    request, which is what makes these routes stateless to their callers: there
    is no handle to leak, no tree to collide with another caller's, and no
    cleanup an interrupted client can skip. What the process keeps across
    requests is the proposal-outcome counts' seen sets and the identity cache
    beside it (`_count_outcomes`), which no answer depends on.
    """

    def __init__(
        self,
        scratch_root: str | Path,
        git_runner: Callable[..., subprocess.CompletedProcess],
        cli_runner: Callable[..., subprocess.CompletedProcess] | None = None,
        refresh: Callable[[str, str], None] | None = None,
        base_branch: str | None = None,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        http_max_bytes: int = DEFAULT_HTTP_MAX_BYTES,
        http_opener: Callable[..., Any] | None = None,
        request_deadline: Callable[[], float | None] | None = None,
        pinned_bases: Mapping[tuple[str, str], str] | None = None,
        tls_refusal: Callable[[str], WorkspaceError | None] | None = None,
        merge_observer: Callable[[int], None] | None = None,
        close_observer: Callable[[int], None] | None = None,
        started_at: float | None = None,
        mint_write: Callable[[str, str], str] | None = None,
        drop_write: Callable[[str], None] | None = None,
        mint: Callable[[str, str], str] | None = None,
        repository_role: Callable[[Forge, str], str] | None = None,
        bare_name_fallback: Callable[[Any, tuple[Forge, ...]], tuple[str, str, str] | str | None] | None = None,
    ) -> None:
        self.scratch_root = Path(scratch_root)
        # Which list of the broker's ConfigMap names `repo` on `forge`: one of
        # the ROLE_* values, or a raise when the lists cannot be read. Only
        # `snapshot` asks. None answers ROLE_UNREGISTERED for every repository,
        # so a broker built without it presents no credential on a snapshot.
        self._repository_role = repository_role
        # Where `snapshot` reads a bare name, asked before the registry with
        # the forges this broker built: a host-qualified name to resolve in
        # its place; or, for a name read from a host the forge table does not
        # hold, with no credential, the forge label, the repository and the
        # clone URL; or None, for the registry to resolve the name as given.
        self._bare_name_fallback = bare_name_fallback
        # git's output to the FORGE_TLS_UNTRUSTED refusal it stands for, or
        # None, for the git runs that read a failure themselves rather than
        # raising it. The broker hands its own, which names a forge's CA
        # Secret; without one, the shared reading names the cause alone.
        self._tls_refusal = tls_refusal or tls_refusal_default
        # The proposal-outcome counts (`_count_outcomes`): each observer is
        # called with how many proposals of this install's own a read just
        # showed in its outcome, merged or closed without a merge, for the
        # first time in this process. None counts nothing. `started_at` is the
        # process start the counts are bounded by, so a restart counts no
        # outcome the previous process could have counted.
        self.merge_observer = merge_observer
        self.close_observer = close_observer
        self.started_at = time.time() if started_at is None else started_at
        self._merged_seen: set[tuple[str, str, Any]] = set()
        self._closed_seen: set[tuple[str, str, Any]] = set()
        self._merged_lock = threading.Lock()
        # The credential's login per (forge, repository), asked once per process
        # and only when a merge could count: the lookup is a forge call. A
        # lookup that raised is not remembered, so an outage is asked again.
        self._merged_viewer: dict[tuple[str, str], str] = {}
        self._merged_nameless_logged = False
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        self._git_runner = git_runner
        self.base_branch = (base_branch or "").strip()
        # (forge host, path) -> the base every proposal onto that repository
        # must target (`_pinned_base`), as `parse_pinned_bases` keys it. Each
        # pinned branch is also one no write door may move, as `base_branch`
        # is, under the name it is stored by.
        self.pinned_bases = dict(pinned_bases or {})
        # A CLI transport needs the broker's credential environment but no
        # repository. When the caller does not separate the two, the git runner
        # serves both.
        # No timeout here on purpose: the runners are given one by whoever
        # built them, and a second number this class merely stored would read
        # like a bound it enforces.
        self._cli_runner = cli_runner or git_runner
        # The in-process transport has no runner to carry its bounds, so the
        # broker hands them over itself; `build_vcs_broker` passes the same
        # timeout and output ceiling the CLI runner enforces.
        self._http_timeout = http_timeout
        self._http_max_bytes = http_max_bytes
        self._http_opener = http_opener
        # The deadline of the request slot the calling thread holds: the bound
        # the CLI runner applies per request, which a per-call timeout is not.
        self._request_deadline = request_deadline
        self.max_clone_bytes = _positive_int(
            "CREDENTIAL_PROXY_MAX_CLONE_BYTES", DEFAULT_MAX_CLONE_BYTES
        )
        self.max_bundle_bytes = max_bundle_bytes()
        self.max_snapshot_bytes = _positive_int(MAX_SNAPSHOT_BYTES_ENV, DEFAULT_MAX_SNAPSHOT_BYTES)
        # The refresh operation is configuration in the sense that matters: it
        # is how this install performs a privileged act, and a forge decides
        # whether its credential strategy has any use for one.
        # `mint_write` is the second: the in-memory write token a forge with no
        # CLI behind it presents itself. `mint` is the third: the read-only
        # token a forge's `read_credential` presents for a context repository's
        # snapshot.
        self.registry = Registry(
            {"refresh": refresh, "mint_write": mint_write, "drop_write": drop_write, "mint": mint}
        )
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
            lambda: self._transport(forge, repo),
            self._git_for(forge, repo),
        )

    def _transport(self, forge: Forge, repo: str) -> Transport:
        """The transport the forge declared, constructed here and never there.

        A forge names what it needs; the broker owns everything about how the
        call is made -- the executable, the timeout, the output ceiling. A CLI
        forge gets the runner; an HTTP forge gets an in-process client bounded
        by the same numbers, presenting its credential's headers for `repo`.
        """
        if forge.transport == "cli" and forge.cli:
            return CliTransport(self._cli_runner, forge.cli, forge.error_overrides)
        if forge.transport == "http" and forge.api_url:
            return HttpTransport(
                forge.api_url,
                lambda: forge.credential.headers(repo),
                forge.error_overrides,
                timeout=self._http_timeout,
                max_bytes=self._http_max_bytes,
                whoami_route=forge.whoami_route,
                whoami_graphql=getattr(forge, "whoami_graphql", ""),
                opener=self._http_opener,
                outer_deadline=self._request_deadline,
                ca_file=forge.ca_file,
                ca_source=forge.ca_source,
                allow_http=(forge.scheme == "http"),
            )
        raise ForgeUnsupported(
            f"{forge.name} declares the {forge.transport!r} transport, which "
            "this broker does not build."
        )

    def credential_reach(self, forge: Forge) -> tuple[list[str], bool] | None:
        """What `forge`'s credential can reach, asked through its own transport.

        None when the forge cannot say. Raises what the transport raises; the
        caller decides what an unanswered question means.
        """
        return forge.reach(self._transport(forge, "").api)

    def _git_for(self, forge: Forge, repo: str) -> Callable[..., Any]:
        """A git runner carrying whatever config this forge needs on it.

        Per-invocation, not global. The broker already forces a config layer
        onto every git it runs; this adds to that layer for one forge's own
        invocations, so a credential belonging to one forge is not installed on
        every git in the process.
        """
        # Read per invocation, not at bind time: a minted credential holds no
        # token until the verb's `ensure` has run, and a token minted later
        # than the bind is the one git must present.
        return self._git_with(
            lambda: tuple(forge.credential.git_config(repo)), forge.scheme == "http"
        )

    def _git_with(
        self, config: Callable[[], tuple[tuple[str, str], ...]], allow_http: bool
    ) -> Callable[..., Any]:
        """A git runner that puts `config()` on each invocation's config layer."""

        def run(cwd: Path, *args: str, check: bool = True, stop: Callable[[], bool] | None = None):
            extra: dict[str, Any] = {}
            if allow_http:
                extra["allow_http"] = True
            if stop is not None:
                extra["stop"] = stop
            return self._git_runner(["git", *args], cwd, check, config(), **extra)

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

    def _enforce_ceiling(
        self, root: Path, repo: str, remedy: str = "Name a `branch` to fetch one line of development."
    ) -> None:
        if _tree_bytes(root, self.max_clone_bytes) > self.max_clone_bytes:
            raise self._clone_too_large(repo, remedy)

    def _clone_too_large(self, repo: str, remedy: str) -> WorkspaceError:
        return WorkspaceError(
            f"{repo} is larger than the {self.max_clone_bytes}-byte "
            f"ceiling for a broker-side clone. {remedy}",
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

    def _pinned_base(self, forge: Forge, repo: str) -> str | None:
        """The branch every proposal onto `repo` must target, or None.

        `repo` is the forge's own reading of the request, which names no host,
        so it is matched on the host the forge lists first, its canonical one,
        which is the host every pin is keyed on. A request spelled with another
        of the forge's hosts is the same repository; the same path on another
        forge is not.
        """
        return pinned_base(self.pinned_bases, forge.hosts[0] if forge.hosts else "", repo)

    def _refuse_off_base(self, forge: Forge, repo: str, target: Any) -> str | None:
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
        base = self._pinned_base(forge, repo)
        if base is None:
            return None
        target = short_branch(validate_branch(target, "target"))
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
        answer["baseBranch"] = self._pinned_base(forge, repo)
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
        refused by name, asked of the remote before anything is cloned.
        `baseBranch` in the answer is the pinned base, or None.
        """
        bound = self._bind(payload)
        base = self._pinned_base(bound.forge, bound.repo)
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
            url = bound.forge.clone_url(bound.repo)
            if branch is None and base is not None:
                # Asked before the clone, which would fetch the whole
                # repository only to be thrown away. A remote that could not
                # be asked falls through to the check after the clone.
                probe = git(
                    root, "ls-remote", "--exit-code", "--heads", url,
                    f"refs/heads/{base}", check=False,
                )
                if probe.returncode == LS_REMOTE_NO_MATCH_EXIT_CODE:
                    raise _base_branch_missing(bound.repo, base)
            argv = ["clone", "--quiet", "--no-recurse-submodules"]
            if branch is not None:
                argv += ["--single-branch", "--branch", branch]
            argv += [url, "."]
            git(root, *argv)
            self._enforce_ceiling(root, bound.repo)
            if branch is None and base is not None:
                present = git(
                    root, "rev-parse", "--verify", "--quiet",
                    f"refs/remotes/origin/{base}", check=False,
                )
                if present.returncode != 0:
                    raise _base_branch_missing(bound.repo, base)
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

    def snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One revision's files, as a tar, for a repository of any role.

        The read for a repository this install does not manage: a context
        repository, or an unregistered public one. A managed repository is
        served too. The clone is bare, one revision deep and one branch wide,
        and it is never checked out; `git archive` writes the tree as a tar
        with no `.git` in it, and the scratch directory is deleted before the
        answer is composed. A tar carries no history, so the shallow boundary
        that makes `clone` refuse `depth` does not arise (`history: "none"`).

        The dispatcher resolves the host before this runs, so the host
        allowlist holds, and does not ask the managed list: the credential is
        chosen here by the repository's role (`_snapshot_git`). `ref` is a
        branch or a tag; with none, a pinned base is used, as `clone` does,
        and then the remote's default branch. `paths` narrows the tar to
        literal repository-relative paths.
        """
        ref = payload.get("ref")
        if ref is not None:
            ref = validate_branch(ref, "ref")
            if _OBJECT_ID.fullmatch(ref):
                raise WorkspaceError(
                    "ref names a revision; a snapshot takes a branch or a tag name"
                )
        paths = _snapshot_paths(payload.get("paths"))
        repository, fallback = self.snapshot_source(payload.get("repository"))
        if fallback is not None:
            forge_name, repo, url = fallback
            base = None
            role = ROLE_UNREGISTERED
            git = self._git_with(lambda: (HELPER_CLEARED,), False)
        else:
            bound = self._bind({"repository": repository})
            forge_name, repo = bound.forge.name, bound.repo
            # Before the role and any credential: a host this install names
            # but serves no credential for refuses here, by name.
            url = bound.forge.clone_url(repo)
            base = self._pinned_base(bound.forge, repo)
            role, git = self._snapshot_git(bound)

        root = self._scratch("snapshot")
        clone_dir = root / SNAPSHOT_CLONE_DIR
        archive = root / SNAPSHOT_ARCHIVE_FILE
        try:
            target = ref or base
            if ref is None and base is not None:
                # Asked before the clone, as `clone` does, so a pinned base
                # the remote lacks is refused by name and not as a git error.
                probe = git(
                    root, "ls-remote", "--exit-code", "--heads", url,
                    f"refs/heads/{base}", check=False,
                )
                if probe.returncode == LS_REMOTE_NO_MATCH_EXIT_CODE:
                    raise _base_branch_missing(repo, base)
            argv = [
                "clone", "--quiet", "--bare", "--depth", "1", "--single-branch",
                "--no-recurse-submodules",
            ]
            if target is not None:
                argv += ["--branch", target]
            argv += [url, SNAPSHOT_CLONE_DIR]
            # Measured while git writes, not only after: the repository can be
            # anyone's, and a clone that ran to the end could fill the scratch
            # volume first. Past the ceiling the clone is killed (checked about
            # every `CLONE_WATCH_SECONDS`). A killed git removes what it wrote,
            # so the watch's own record, not a second measure, answers the 413.
            last, stopped = [0.0], [False]

            def over_ceiling() -> bool:
                now = time.monotonic()
                if now - last[0] < CLONE_WATCH_SECONDS:
                    return False
                last[0] = now
                stopped[0] = _tree_bytes(clone_dir, self.max_clone_bytes) > self.max_clone_bytes
                return stopped[0]

            cloned = git(root, *argv, check=False, stop=over_ceiling)
            # One revision, one branch, no history: no field of the request
            # makes this clone smaller, and `paths` narrows only the tar.
            remedy = "The pack of this one revision is over it; no `ref` or `paths` makes the clone smaller."
            if stopped[0]:
                raise self._clone_too_large(repo, remedy)
            if cloned.returncode != 0:
                raise subprocess.CalledProcessError(cloned.returncode, argv, cloned.stdout, cloned.stderr)
            self._enforce_ceiling(clone_dir, repo, remedy=remedy)
            if target is None:
                target = self._default_branch(git, clone_dir)
            revision = self._snapshot_revision(git, clone_dir, target)
            attributes = clone_dir / SNAPSHOT_ATTRIBUTES_FILE
            attributes.parent.mkdir(parents=True, exist_ok=True)
            attributes.write_text(SNAPSHOT_ATTRIBUTES, encoding="utf-8")
            self._measure_snapshot(git, clone_dir, revision, paths, repo)
            # `--literal-pathspecs` is GIT_LITERAL_PATHSPECS as a flag: each
            # path is a name, never a glob or pathspec magic.
            argv = [
                "--literal-pathspecs", "archive", "--format=tar",
                f"--output={archive}", revision,
            ]
            if paths:
                argv += ["--", *paths]
            written = git(clone_dir, *argv, check=False)
            if written.returncode != 0:
                if paths and PATHSPEC_NO_MATCH in (written.stderr or ""):
                    # The revision goes with the refusal: a caller that asked
                    # for an optional folder still learns which commit it read.
                    raise WorkspaceError(
                        f"a path in `paths` names nothing in {repo} at {target}",
                        code="PATH_NOT_FOUND",
                        revision=revision,
                    )
                raise subprocess.CalledProcessError(
                    written.returncode, argv, written.stdout, written.stderr
                )
            size = archive.stat().st_size
            # The second check: the measure above is the one that keeps a
            # large tar off the disk.
            if size > self.max_snapshot_bytes:
                raise self._snapshot_too_large(repo, size)
            blob = base64.b64encode(archive.read_bytes()).decode("ascii")
        finally:
            _remove_tree(root)
        return {
            "forge": forge_name,
            "repo": repo,
            "role": role,
            "ref": target,
            "revision": revision,
            "size": size,
            "archiveBase64": blob,
            "history": SNAPSHOT_HISTORY,
        }

    def _snapshot_too_large(self, repo: str, size: int) -> WorkspaceError:
        return WorkspaceError(
            f"the snapshot of {repo} is {size} bytes, over the "
            f"{self.max_snapshot_bytes}-byte ceiling. Name `paths` to "
            "take part of the tree.",
            status=413,
            code="SNAPSHOT_TOO_LARGE",
        )

    def _measure_snapshot(
        self, git: Callable, clone_dir: Path, revision: str, paths: list[str], repo: str
    ) -> None:
        """Refuse a tree whose tar would pass the ceiling, before `git archive` writes it.

        The clone ceiling measures the pack, and a pack compresses: a few MB
        of repeated bytes can be GB as a tar, and writing that to the scratch
        volume is what would get the broker evicted. So the tar is sized from
        the tree listing (`_tar_bytes`) first. A listing the runner cut short
        is refused too: a partial sum says nothing about the rest.
        """
        argv = ["--literal-pathspecs", "ls-tree", "-r", "-t", "-z", SNAPSHOT_LISTING_FORMAT, revision]
        if paths:
            argv += ["--", *paths]
        listed = git(clone_dir, *argv)
        if getattr(listed, "truncated", False):
            raise WorkspaceError(
                f"the tree of {repo} lists more entries than one answer holds. "
                "Name `paths` to take part of the tree.",
                status=413,
                code="SNAPSHOT_TOO_LARGE",
            )
        size = _tar_bytes(listed.stdout or "")
        if size > self.max_snapshot_bytes:
            raise self._snapshot_too_large(repo, size)

    def snapshot_source(self, repository: Any) -> tuple[Any, tuple[str, str, str] | None]:
        """The repository `snapshot` resolves, and the read outside the host table, if any.

        `bare_name_fallback` answers for a bare name: a host-qualified name
        resolved in its place, or `(forge label, repository, clone URL)` for a
        read with no credential that is not resolved at all. Asked by
        `snapshot` and by the HTTP layer, which resolves the first answer and
        skips its resolve for the second.
        """
        if self._bare_name_fallback is None:
            return repository, None
        answer = self._bare_name_fallback(repository, self.registry.forges)
        if isinstance(answer, str):
            return answer, None
        return repository, answer

    def _snapshot_git(self, bound: Binding) -> tuple[str, Callable[..., Any]]:
        """The repository's role, and a git runner that presents its credential.

        - managed: the forge's managed credential, made current, as `clone`
          presents it;
        - context: the forge's read-only credential, never the write one, with
          the ambient helper cleared. A forge with no read-only credential
          presents none;
        - unregistered, or lists that cannot be read: no credential, with the
          ambient helper cleared. A forge token can often read more than the
          install lists, so a private repository nobody registered fails at
          the clone rather than being read on it.

        The clear is added here because `NoCredential` presents an empty
        config, which leaves the ambient helper in place.
        """
        forge, repo = bound.forge, bound.repo
        allow_http = forge.scheme == "http"
        role = ROLE_UNREGISTERED
        if self._repository_role is not None:
            try:
                role = self._repository_role(forge, repo)
            except Exception as exc:  # noqa: BLE001 - read as unregistered
                LOGGER.warning(
                    "snapshot repo=%s: the repository lists could not be read "
                    "type=%s; cloning without a credential",
                    repo,
                    type(exc).__name__,
                )
                role = ROLE_UNREGISTERED
        LOGGER.info("snapshot credential forge=%s repo=%s role=%s", forge.name, repo, role)
        if role == ROLE_MANAGED:
            bound.ensure()
            return role, bound.git
        if role == ROLE_CONTEXT:
            credential = forge.read_credential(repo)
            warn_without_read_token(forge, credential, f"snapshot credential repo={repo}")
            credential.ensure(repo)
            config = tuple(credential.git_config(repo))
            if HELPER_CLEARED not in config:
                config = (HELPER_CLEARED, *config)
            return role, self._git_with(lambda: config, allow_http)
        return ROLE_UNREGISTERED, self._git_with(lambda: (HELPER_CLEARED,), allow_http)

    @staticmethod
    def _snapshot_revision(git: Callable, clone_dir: Path, target: str) -> str:
        """The commit `target` names in a bare single-branch clone.

        Looked up by namespace rather than through HEAD: a bare clone of a tag
        leaves HEAD on no branch.
        """
        for namespace in SNAPSHOT_REF_NAMESPACES:
            found = git(
                clone_dir, "rev-parse", "--verify", "--quiet",
                f"{namespace}{target}^{{commit}}", check=False,
            )
            revision = (found.stdout or "").strip()
            if found.returncode == 0 and revision:
                return revision
        raise WorkspaceError(
            f"the clone holds no branch or tag {target}",
            status=502,
            code="GIT_FAILED",
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
            self._refuse_off_base(bound.forge, bound.repo, target)
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
            refusal = self._tls_refusal(listed.stderr or "")
            if refusal is not None:
                raise refusal
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
        except IdentityUnknown:
            # Not a transient failure: no retry names the account. Refused
            # with its own code, as the `advance` check does.
            raise
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
        operator-configured base override, every pinned base on every
        repository, and any run/** branch. `publish` refuses a direct push to
        one and `branch-delete` refuses to remove one.
        `root` must already have `origin`.
        """
        default = self._default_branch_of_remote(git, root, self._tls_refusal)
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
        # A pin is stored in its one canonical spelling, so it is protected
        # under exactly the name proposals target, not re-read here.
        protected_branches.update(
            pinned.casefold() for pinned in self.pinned_bases.values()
        )

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
    def _default_branch_of_remote(
        git: Callable,
        root: Path,
        tls_refusal: Callable[[str], WorkspaceError | None] = tls_refusal_default,
    ) -> str:
        """The branch the remote's HEAD points at, or "" if it says nothing.

        `ls-remote --symref` prints `ref: refs/heads/<name>\tHEAD` first when
        the remote advertises a symbolic HEAD. A remote that advertises none --
        an empty repository, or a server that hides it -- yields "", and the
        caller treats that as "no default to protect" rather than as a
        refusal, because there is nothing to compare against. A certificate
        that fails is not "says nothing": it is raised by its cause.
        """
        listed = git(root, "ls-remote", "--symref", "origin", "HEAD", check=False)
        if getattr(listed, "returncode", 0) != 0:
            refusal = tls_refusal(listed.stderr or "")
            if refusal is not None:
                raise refusal
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
        except IdentityUnknown:
            # The forge cannot name this install's account, and no retry
            # changes that: refused with its own code, which names the fix.
            raise
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
            return bound.whoami()
        except ForgeUnsupported:
            return ""

    def _forge_verb(
        self,
        verb: str,
        payload: dict[str, Any],
        pinned_target: bool = False,
        observe: Callable[[Binding, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        bound = self._bind(payload)
        # Before anything else that could spend a call: a forge with no labels
        # refuses a label field by name, so a filter is not silently ignored
        # and a label write does not look done. Only for a verb the forge
        # serves (and `label-ensure`, which is the labels gap itself): an issue
        # verb on a forge with no issue tracker is refused for that, which is
        # the real gap, before its label field is looked at.
        if verb == "label-ensure" or verb in bound.forge.verbs:
            bound.forge.refuse_labels(verb, payload)
        if pinned_target:
            # Before the method is looked up and before `bound.api`, so a
            # refused target makes no forge call and spends no credential.
            base = self._refuse_off_base(bound.forge, bound.repo, payload.get("target"))
            if base is not None:
                payload = {**payload, "target": base}
        method = getattr(bound.forge, verb.replace("-", "_"))
        result = bound.stamp(method(bound.api, bound.repo, payload))
        if observe is not None:
            # A reader of the answer, never a party to it: whatever it does
            # or fails to do, the caller gets the forge's answer.
            observe(bound, result)
        return result

    def proposal_create(self, payload):
        return self._forge_verb("proposal-create", payload, pinned_target=True)

    def proposal_list(self, payload):
        return self._forge_verb("proposal-list", payload, observe=self._observe_outcomes_list)

    def proposal_view(self, payload):
        return self._forge_verb("proposal-view", payload, observe=self._observe_outcomes_view)

    # -- the proposal-outcome counts ---------------------------------------
    #
    # The agent never merges, and closes only through `proposal-close` here,
    # which is marked seen; any other merge or close is a person's, a forge
    # rule's or a bot's act on the forge, and the broker learns of it only
    # when a later read shows the proposal in that state. The counts therefore ride on the reads the agent
    # already makes of its proposals, under three rules that keep them from
    # counting an outcome twice without a store: the proposal is this
    # install's own (its author is the credential's login), it reached the
    # outcome no earlier than this process started (one before that was the
    # previous process's to count, or is lost with it), and each proposal is
    # counted once per outcome per process. An outcome this process never
    # reads is not counted: the counts under-count rather than over-count,
    # like the operator's totals they feed. A proposal closed, reopened and
    # merged counts once in each.

    def _observe_outcomes_list(self, bound: Binding, result: dict[str, Any]) -> None:
        proposals = result.get("proposals")
        self._count_outcomes(bound, proposals if isinstance(proposals, list) else [])

    def _observe_outcomes_view(self, bound: Binding, result: dict[str, Any]) -> None:
        proposal = result.get("proposal")
        self._count_outcomes(bound, [proposal] if isinstance(proposal, dict) else [])

    def _note_own_close(self, bound: Binding, result: dict[str, Any], number: Any) -> None:
        """Mark a proposal as closed by this broker, so no read counts it."""
        proposal = result.get("proposal") if isinstance(result, dict) else None
        if isinstance(proposal, dict) and proposal.get("number") is not None:
            number = proposal.get("number")
        if number is None:
            return
        with self._merged_lock:
            self._closed_seen.add(self._merged_key(bound, number))

    def _count_outcomes(self, bound: Binding, proposals: list) -> None:
        try:
            self._count_outcome(bound, proposals, MERGED_PROPOSAL_STATE, self.merge_observer, self._merged_seen)
            self._count_outcome(bound, proposals, CLOSED_PROPOSAL_STATE, self.close_observer, self._closed_seen)
        except Exception as exc:  # noqa: BLE001 -- the count never fails the read
            LOGGER.debug("proposal-outcome count skipped type=%s", type(exc).__name__)

    def _count_outcome(
        self,
        bound: Binding,
        proposals: list,
        state: str,
        observer: Callable[[int], None] | None,
        seen: set[tuple[str, str, Any]],
    ) -> None:
        if observer is None:
            return
        # The free rules first -- in the state, not before this process
        # started, not already counted -- so the identity lookup, a forge call,
        # runs only when something could count, and never for a history of old
        # outcomes a `state=all` listing carries on every read.
        with self._merged_lock:
            candidates = [
                p for p in proposals
                if isinstance(p, dict)
                and p.get("state") == state
                and (closed := _iso_timestamp(p.get("closed"))) is not None
                and closed >= self.started_at
                and self._merged_key(bound, p.get("number")) not in seen
            ]
        if not candidates:
            return
        viewer = self._merged_viewer_for(bound)
        if not viewer:
            return
        count = 0
        with self._merged_lock:
            for proposal in candidates:
                if _login_key(str(proposal.get("author") or "")) != _login_key(viewer):
                    continue
                key = self._merged_key(bound, proposal.get("number"))
                if key in seen:
                    continue
                seen.add(key)
                count += 1
        if count:
            observer(count)

    @staticmethod
    def _merged_scope(forge: Forge) -> str:
        """What the merged count and its identity cache are keyed on besides
        the repository: the forge's canonical host, as `_pinned_base` keys the
        pins, since one provider serves several hosts (two forges of one
        provider on different hosts carry the same `name` and may carry the
        same path) and the same path on another host is another repository
        with its own credential."""
        return forge.hosts[0] if forge.hosts else forge.name

    @classmethod
    def _merged_key(cls, bound: Binding, number: Any) -> tuple[str, str, Any]:
        """The seen-set key: forge host, repository and number. The repository
        is casefolded, since a caller may spell `Acme/Infra` and `acme/infra`
        for the one repository the forge resolves them to, and the count is per
        proposal, not per spelling."""
        return (cls._merged_scope(bound.forge), bound.repo.casefold(), number)

    def _merged_viewer_for(self, bound: Binding) -> str:
        """The credential's login for the outcome counts, remembered per repository,
        keyed the way the seen set is: the login is the credential's, not the
        spelling's.

        "" is a real answer, a credential that cannot name itself, and is
        remembered too, with one INFO line per process so that a flat
        `remediationsMergedTotal` and `remediationsClosedUnmergedTotal` on such
        an install are explained in the log;
        a lookup that raises is left to the caller's guard and asked again next
        time.
        """
        key = (self._merged_scope(bound.forge), bound.repo.casefold())
        with self._merged_lock:
            if key in self._merged_viewer:
                return self._merged_viewer[key]
        viewer = self._viewer(bound)
        with self._merged_lock:
            self._merged_viewer[key] = viewer
            announce = not viewer and not self._merged_nameless_logged
            if announce:
                self._merged_nameless_logged = True
        if announce:
            LOGGER.info(
                "proposal-outcome counts are off for %s: the credential cannot name itself, "
                "so no proposal can be told to be this install's own",
                self._merged_scope(bound.forge),
            )
        return viewer

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
        # A close this route makes is the harness tidying up (the fleet audit
        # closes its own stale remediation proposals here), not a person
        # rejecting: it is marked seen before the forge call, because the
        # forge applies the close before it answers, and an answer lost to a
        # timeout or a reset would otherwise leave a close of the broker's own
        # for a later read to count as a person's. A close the forge refused
        # leaves the proposal open; if a person closes it later, that close is
        # not counted, the under-counting direction every counter here takes.
        # The number is validated here first, as the adapter would, so a number
        # the forge never issued, or a bool that hashes like 1, leaves no mark.
        number = validate_number(payload.get("number")) if isinstance(payload, dict) else None
        bound = self._bind(payload) if number is not None else None
        if bound is not None:
            self._note_own_close(bound, {}, number)
        try:
            return self._forge_verb("proposal-close", payload)
        except WorkspaceError as exc:
            # The forge refused and applied nothing: the mark comes off, so a
            # person's later close of this proposal counts. A lost answer (502,
            # 503) keeps it: the forge may have applied the close.
            status = getattr(exc, "status", None)
            if bound is not None and isinstance(status, int) and status < FORGE_REFUSAL_STATUS_CEILING:
                with self._merged_lock:
                    self._closed_seen.discard(self._merged_key(bound, number))
            raise

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
        viewer = bound.whoami()
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


# The verbs that leave a mark on the forge. Named beside the route table because
# that is where a new verb gets added, and a verb added to one and not the other
# is the mistake this placement is meant to make loud. Every one of them, like
# every read below, is refused at the HTTP layer for a repository this install
# does not manage; the set is kept because a write is what that refusal exists
# for, and the classification test reads it.
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


# The verbs the HTTP layer answers for a repository this install does not
# manage. `capabilities` reports what a forge serves and spends no credential.
# Every other verb, read or write, is refused at the route first: reads used to
# be refused only because the one shipped credential asked the managed list
# while refreshing, and a credential that does not refresh -- a static token
# scoped to a whole group -- would have been spent on any repository in it.
# Reading a repository this install does not manage goes through the one
# verb in ROLE_SELECTED_VERBS below; for every other verb the managed list is
# a visibility control as well as a write one, by design rather than by
# accident of one forge.
UNGATED_VERBS = frozenset({"capabilities"})

# The verbs that serve a repository of any role and choose the credential by
# that role themselves. The HTTP layer still resolves the repository, so the
# host allowlist holds, but does not ask the managed list. `snapshot` presents
# the managed credential for a managed repository, the forge's read-only one
# for a context repository, and none for any other (`VcsBroker._snapshot_git`).
# Never a write: a write is what the managed list exists for.
ROLE_SELECTED_VERBS = frozenset({"snapshot"})

# Every verb `route_table` serves, as a constant: the closed vocabulary the
# broker's version-control counter labels a request under, so a caller's path
# never names a series. `test_the_verb_vocabulary_is_the_route_table` holds the
# two in step.
VCS_VERBS = frozenset(
    {
        "capabilities",
        "clone",
        "snapshot",
        "publish",
        "proposal-create",
        "proposal-list",
        "proposal-view",
        "proposal-comment",
        "issue-create",
        "issue-list",
        "issue-view",
        "issue-comment",
        "proposal-update",
        "proposal-close",
        "proposal-commits",
        "proposal-acknowledge",
        "issue-update",
        "issue-close",
        "label-ensure",
        "identity",
        "branch-view",
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
        "snapshot": broker.snapshot,
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
    "VCS_VERBS",
    "AGENT_BRANCH_PREFIX",
    "ROLE_CONTEXT",
    "ROLE_MANAGED",
    "ROLE_SELECTED_VERBS",
    "ROLE_UNREGISTERED",
    "UNGATED_VERBS",
    "WRITE_VERBS",
    "max_bundle_bytes",
    "route_table",
]
