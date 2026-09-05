#!/usr/bin/env python3
"""One private git clone per concurrent operation, so agents stop stomping.

The pod does not run in a checkout. `hermes run` starts an agent in its profile
directory, and nothing clones the GitOps repository — so every `git` the skills
issued was landing outside a working tree. This module establishes the clone
lazily, on the first run that needs it.

Why the clone is *leased*
-------------------------
The first version of this file put every repository at one flat path, a pure
function of `owner/name`. That is exactly one working tree for the whole pod,
and the pod runs many agents at once: six audit crons, plus every kanban worker
the dispatcher spawns, plus whatever the operator is doing interactively. In the
incident that prompted this design, the `submit-suggestion` skill ran
`git checkout -b …` and `git push -f` inside the tree a fleet audit was midway
through using, because neither skill named a directory and both defaulted to the
same one.

Serialising that with a lock cannot work, for two independent reasons:

* The window that needs protecting spans processes. A fleet audit writes its
  remediation manifests into the tree *between* `audit_report.py start` and
  `audit_report.py finish` — minutes apart, two separate invocations. An
  `fcntl.flock` fd dies with the process that opened it, so it cannot span them.
* Even if it could, serialising is the wrong answer. Concurrent work by
  different agents is the steady state here, not an anomaly; a ten-minute audit
  must not block an interactive provisioning request.

So each concurrent operation takes a **lease** and gets its own clone under it:

    <root>/<lease>/<owner>__<name>

The lease is a caller-chosen, stable string — the audit id for a fleet audit
stream, the kanban task id for a dispatcher-spawned worker. Being stable is what
lets `start` and `finish` find the same tree with no lookup state; being
per-operation is what keeps two operations out of each other's way. Nobody
waits on anybody.

`<root>/<lease>/.lease` records who holds it. It is the reaper's TTL anchor (its
mtime is refreshed on every `ensure_workspace`), the marker the credential proxy
looks for before it will run a tree-mutating `git`, and the record a client
checks before writing in a tree that might not be its own.

Everything here runs through an injected `runner` so the caller's logging, error
handling and credential proxying apply unchanged, and so the existing test
harness sees these calls like any other.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator

DEFAULT_AGENT_HOME = "/opt/data"


def agent_home() -> str:
    """The agent's data root — which is not always `/opt/data`.

    `PlatformAgent.spec.harness.hermes.agentHome` moves it, and the operator
    then writes that same value into two places: `PLATFORM_AGENT_HOME` on the
    agent container and `CREDENTIAL_PROXY_WORKSPACE_ROOT` on the sidecar
    (`platformagent_manifests.go`). Reading the environment is what keeps this
    module's idea of the root and the proxy's containment check the same string.
    A clone under a hardcoded `/opt/data` on a deployment whose home is
    elsewhere is *outside* the sidecar's workspace root, and every `git` in it
    is refused — the whole skill fails, with an error about containment rather
    than about the constant that drifted.

    Deliberately not `HERMES_HOME`. In the agent container that names the
    *profile* home — `<agent home>/profiles/platform` for a platform worker —
    so it points a level or two too deep.
    """
    return (os.environ.get("PLATFORM_AGENT_HOME") or DEFAULT_AGENT_HOME).rstrip(
        "/"
    ) or "/"


def default_root() -> str:
    """Where leased clones live: `<agent home>/gitops`.

    Must be under the credential proxy's workspace root (the shared PVC). The
    sidecar executes `git` and `gh` in *its own* filesystem at the cwd the
    client reports, and refuses any path outside that root — so a clone in
    /tmp, which is a per-container emptyDir, is invisible to the process that
    would have to run git in it.
    """
    return str(Path(agent_home()) / "gitops")

# The name the credential proxy also looks for. Changing it here without
# changing `credential_proxy._GIT_LEASE_MARKER` locks every skill out of git.
LEASE_FILENAME = ".lease"

# How long a lease directory may go untouched before the next caller reclaims
# its disk. Generous on purpose: `ensure_workspace` refreshes the marker on
# every call, so the only trees this reaps are ones whose owner died. A run that
# somehow straddles the TTL loses its untracked manifests and re-clones, which
# is the same outcome a crashed run already had.
DEFAULT_LEASE_TTL_HOURS = 24.0
# The branch a pull request targets when nothing better can be determined —
# which is only when there is no clone to ask yet. See `resolve_base_branch`.
DEFAULT_BASE_BRANCH = "main"


class GitOpsRepoEmpty(RuntimeError):
    """Raised when a GitOps repository has no commits on any branch."""


# Tolerates the operator's Markdown bullet and bold markers, and the literal
# `None` when the CR leaves it unset.
SETTINGS_REPO_RE = re.compile(r"^\s*[-*]?\s*\**Git Repo:\**\s*(\S+)\s*$", re.M)
_LEASE_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_LEASE_CHARS = 64
BARE_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

Runner = Callable[..., object]
# One `git symbolic-ref` per clone per process, keyed by workspace path. The
# answer cannot change while a process runs, and the call is not free: `git`
# here is a shim that POSTs to the sidecar, so every repeat is an HTTP round
# trip and a line of log noise. Tests that switch repositories clear it.
_BASE_BRANCH_CACHE: dict[str, str] = {}


def forget_base_branch(workspace: str | Path | None = None) -> None:
    """Drop the cached default branch for `workspace`, or for every workspace."""
    if workspace is None:
        _BASE_BRANCH_CACHE.clear()
    else:
        _BASE_BRANCH_CACHE.pop(str(Path(workspace)), None)


def resolve_base_branch(
    workspace: str | Path | None = None, runner: Runner | None = None
) -> str:
    """The branch a pull request should target, for *this* repository.

    Hardcoding `main` was wrong in a quiet way. This harness clones the target
    GitOps repository, and a fleet whose GitOps repo still calls
    its trunk `master` got `origin/main` — a ref that does not resolve. Every
    remediation branch then failed at checkout, and the audit reported the fix
    it could not push as a fix the model never wrote.

    Resolution order:

    1. `GITOPS_BASE_BRANCH`. For a repository whose default branch is not the
       branch the fleet deploys from — a `release` line, say. Nothing this
       function can observe would tell it that, so an operator has to.
    2. `origin/HEAD` in the clone. `git clone` sets it from the default the
       remote advertises, which is the right answer for every ordinary
       repository, and it costs one `symbolic-ref`.
    3. `main`, when there is no clone to ask yet.
    """
    override = os.environ.get("GITOPS_BASE_BRANCH", "").strip()
    if override:
        return override
    if workspace is None:
        return DEFAULT_BASE_BRANCH
    key = str(Path(workspace))
    cached = _BASE_BRANCH_CACHE.get(key)
    if cached:
        return cached
    resolved = _detect_base_branch(workspace, runner) or DEFAULT_BASE_BRANCH
    _BASE_BRANCH_CACHE[key] = resolved
    return resolved


def _detect_base_branch(workspace: str | Path, runner: Runner | None) -> str | None:
    run = runner or _plain_runner
    head = _origin_head(run, workspace)
    if head:
        return head
    # `git clone` sets origin/HEAD, but a clone this module made before it
    # started asking, or a remote that changed its default afterwards, leaves
    # the ref absent or stale. `set-head --auto` re-asks the remote. Not fatal
    # if it fails: a repository with no reachable remote is a reason to fall
    # back to the default, not to abort the run.
    run(["git", "remote", "set-head", "origin", "--auto"], cwd=str(workspace), check=False)
    return _origin_head(run, workspace)


def _origin_head(run: Runner, workspace: str | Path) -> str | None:
    result = run(
        ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd=str(workspace),
        check=False,
    )
    if getattr(result, "returncode", 1) != 0:
        return None
    ref = (getattr(result, "stdout", "") or "").strip()
    # `--short` renders the ref as `origin/main`; callers want the branch name
    # on its own, because they compose both `origin/<branch>` and a bare
    # `--base <branch>` from it.
    name = ref.split("/", 1)[1] if ref.startswith("origin/") else ref
    return name.strip() or None


def _plain_runner(cmd: list[str], *, cwd: str | Path | None = None, check: bool = True):
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, check=check, capture_output=True, text=True
    )


def lease_ttl_hours() -> float:
    raw = os.environ.get("GITOPS_LEASE_TTL_HOURS", "").strip()
    if not raw:
        return DEFAULT_LEASE_TTL_HOURS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_LEASE_TTL_HOURS


def sanitize_lease(lease: str) -> str:
    """A lease id reduced to something safe to use as one path segment.

    The lease reaches this module from an environment variable and, for
    `submit-suggestion`, from an agent-supplied flag. It becomes a directory
    name directly under the shared root, so `../../etc` or a bare `..` has to
    be impossible rather than merely unlikely.
    """
    cleaned = _LEASE_SAFE_RE.sub("-", str(lease or "").strip()).strip("-.")[
        :_MAX_LEASE_CHARS
    ]
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"unusable lease id {lease!r}")
    return cleaned


def session_lease() -> str | None:
    """The lease this session is identified by, or None if it has no identity.

    Separate from `lease_id` because the difference matters to a caller that
    runs *second*. `lease_id` always answers, minting `adhoc-<random>` when
    there is nothing to key off — correct for the process that takes the lease,
    and a trap for the process that has to find it again. A `submit` that
    minted its own would compare two unrelated random strings and refuse the
    tree `prepare` had just handed it, identically on every retry.
    """
    for candidate in (
        os.environ.get("HERMES_KANBAN_TASK"),
        os.environ.get("HERMES_SESSION_ID"),
    ):
        if candidate and str(candidate).strip():
            return sanitize_lease(candidate)
    return None


def lease_id(explicit: str | None = None) -> str:
    """Resolve the lease this process should work under.

    The identifier has to be stable across invocations, because the agent runs
    each shell command in a fresh process: a pid would hand `git commit` and the
    `submit` that follows it two different clones. `HERMES_KANBAN_TASK` is
    pinned into every dispatcher-spawned worker and is exactly the granularity
    wanted — one card, one unit of work, one tree.
    """
    if explicit and str(explicit).strip():
        return sanitize_lease(explicit)
    inherited = session_lease()
    if inherited:
        return inherited
    # No session identity to key off. A fresh lease is still correct — it is
    # isolated from everyone else, which is the point — it just is not
    # recoverable by a later process that did not keep the path. Callers that
    # run second must use `session_lease` and say so, rather than mint one here.
    return f"adhoc-{os.urandom(4).hex()}"


def lease_dir(root: str | Path, lease: str) -> Path:
    return Path(root) / sanitize_lease(lease)


def workspace_path(
    repo: str, root: str | Path | None = None, *, lease: str
) -> Path:
    """Where `owner/name` is cloned for `lease`. One clone per lease, per repo."""
    owner, _, name = str(repo).partition("/")
    if not owner or not name:
        raise ValueError(f"expected a repository as owner/name, got {repo!r}")
    return lease_dir(root if root is not None else default_root(), lease) / f"{owner}__{name}"


@contextmanager
def workspace_lock(root: str | Path | None = None) -> Iterator[None]:
    """Serialise the shared *bookkeeping* under the root — nothing more.

    An advisory `flock` on a file beside the lease directories. What it guards
    is small and fast by design: reaping expired leases, creating a lease
    directory, and writing its marker. It deliberately does **not** span the
    clone, the fetch, or the caller's work — those happen inside a lease nobody
    else can name, so there is nothing left to serialise, and holding a lock
    across them would reintroduce the queueing this layout exists to avoid.

    Best-effort: if the lock file cannot be created — a read-only or absent PVC
    — the caller proceeds unserialised rather than refusing to run. A missed
    lock costs a retry; a refused run costs the day's audit.
    """
    root = Path(root if root is not None else default_root())
    handle = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle = open(root / ".lock", "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        if handle is not None:
            handle.close()
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def read_lease(holder: str | Path) -> dict | None:
    """The lease record in `holder`, or None if there is not a readable one."""
    try:
        text = (Path(holder) / LEASE_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def write_lease(
    holder: str | Path,
    lease: str,
    repo: str | None = None,
    *,
    owner: str | None = None,
) -> dict:
    """Stamp (or refresh) the lease marker in `holder`.

    Refreshing keeps the original `created_at` so the record still says when the
    work started, and rewrites the file so its mtime — which is what the reaper
    reads — moves forward.
    """
    holder = Path(holder)
    previous = read_lease(holder) or {}
    record = {
        "lease": sanitize_lease(lease),
        "owner": owner or previous.get("owner") or "unknown",
        "repo": repo or previous.get("repo"),
        "created_at": previous.get("created_at") or _now_iso(),
        "refreshed_at": _now_iso(),
        "pid": os.getpid(),
    }
    holder.mkdir(parents=True, exist_ok=True)
    (holder / LEASE_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    return record


def lease_holder(path: str | Path) -> Path | None:
    """The nearest ancestor of `path` carrying a lease marker, or None.

    Mirrors `credential_proxy._lease_holder`, which walks the ancestors rather
    than looking only one level up — and has to, because the working directory
    it is handed is whatever the caller reported. So does this: `submit` takes
    `--workspace`, defaulting to `os.getcwd()`, and an agent that has `cd`'d
    into `manifests/` to write a file is two levels below the tree the marker
    sits beside. Checking only the immediate parent would refuse exactly the
    calls the proxy lets through, and the skill would be the thing that broke.

    The walk stops at the filesystem root. A marker anywhere above the path is
    a real lease over it — that is the same rule the proxy applies, bounded
    there by its workspace root rather than by `/`, and reaching `/` here means
    the caller was outside any workspace at all.
    """
    start = Path(path).resolve()
    for directory in (start, *start.parents):
        try:
            if (directory / LEASE_FILENAME).is_file():
                return directory
        except OSError:
            break
    return None


def assert_lease_owner(workspace: str | Path, lease: str) -> dict:
    """Refuse to act inside a working tree this lease does not hold.

    The credential proxy can tell that a `git push` is happening inside *some*
    lease, but not whose — the shim reports argv and a working directory, not a
    caller identity. That last mile is here: a skill that was handed a path
    checks the marker above it before it writes, which is the check that would
    have stopped `submit-suggestion` from branching inside a running audit's
    tree.
    """
    holder = lease_holder(workspace)
    record = read_lease(holder) if holder is not None else None
    if record is None:
        raise PermissionError(
            f"{workspace} is not inside a leased GitOps workspace (no "
            f"{LEASE_FILENAME} in it or in any directory above it). Run the "
            "skill's `prepare` step to get a workspace of your own instead of "
            "writing in a shared clone."
        )
    held = str(record.get("lease", ""))
    if held != sanitize_lease(lease):
        raise PermissionError(
            f"{workspace} belongs to lease {held!r} (owner {record.get('owner')!r}), "
            f"not to {sanitize_lease(lease)!r}. Another agent is working in that "
            "tree; run the skill's `prepare` step to get your own."
        )
    return record


def reap_stale_leases(
    root: str | Path,
    *,
    ttl_hours: float | None = None,
    keep: Iterable[str] = (),
) -> list[str]:
    """Delete lease directories nobody has touched inside the TTL.

    Only directories holding a `.lease` marker are ever considered, which is
    what makes this safe to run under a root shared with anything else: the
    legacy flat `<root>/<owner>__<name>` clone from before leases existed, the
    lock file, and any directory a human made are all invisible to it.
    """
    root = Path(root)
    ttl = lease_ttl_hours() if ttl_hours is None else ttl_hours
    if ttl <= 0:
        return []
    spared = {sanitize_lease(name) for name in keep if name}
    cutoff = time.time() - ttl * 3600.0
    removed: list[str] = []
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.name in spared or not entry.is_dir() or entry.is_symlink():
            continue
        marker = entry / LEASE_FILENAME
        try:
            if not marker.is_file() or marker.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        _remove_tree(entry)
        if not entry.exists():
            removed.append(entry.name)
    return removed


def _is_clone(path: Path) -> bool:
    return (path / ".git").exists()


def ensure_workspace(
    repo: str,
    runner: Runner,
    *,
    lease: str,
    root: str | Path | None = None,
    base_branch: str | None = None,
    remote_url: str | None = None,
    reset: bool = True,
    owner: str | None = None,
) -> Path:
    """Return this lease's clone of `repo`, creating it if it is not there yet.

    With `reset=True` the working tree is returned scrubbed and positioned on
    `base_branch` at the remote's tip, which is what an audit wants before it
    starts: a leftover branch or a dirty tree from a run that crashed is not
    authored by a human and there is nothing in it to preserve. `base_branch`
    defaults to whatever the remote says its default branch is, resolved after
    the clone by `resolve_base_branch` — a repository whose trunk is `master`
    used to fail here on a `origin/main` that does not exist.

    With `reset=False` the tree is left exactly as the caller found it and only
    `origin` is fetched. This is not a nicety — it is the difference between a
    working Tier 2 and a dead one. The agent writes its remediation manifests
    into this tree *between* `start` and `finish`, and those files are untracked
    until the remediation branch stages them, so a `git clean -fd` on the way
    into `finish` deletes every fix the audit just produced and the run reports
    them all as "the file was never written".

    Raises `GitOpsRepoEmpty` if the target repository has zero commits on any
    branch, preventing dispatch runs from crashing on raw git fatal errors.
    """
    root = Path(root if root is not None else default_root())
    lease = sanitize_lease(lease)
    holder = lease_dir(root, lease)
    target = workspace_path(repo, root, lease=lease)
    url = remote_url or f"https://github.com/{repo}.git"

    # Short and shared: everything after it happens inside a directory no other
    # caller will name.
    with workspace_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        reap_stale_leases(root, keep={lease})
        write_lease(holder, lease, repo, owner=owner)

    if not _is_clone(target):
        # A partial directory from a clone that died mid-transfer is not a
        # working tree and will never become one; clear it rather than letting
        # `git clone` refuse a non-empty destination forever.
        if target.exists():
            _remove_tree(target)
        runner(["git", "clone", "--quiet", url, str(target)], cwd=str(holder))
        if not _is_clone(target):
            raise RuntimeError(f"clone of {repo} into {target} produced no working tree")

    runner(["git", "remote", "set-url", "origin", url], cwd=str(target), check=False)
    runner(["git", "fetch", "--quiet", "--prune", "origin"], cwd=str(target))
    if not reset:
        return target

    # Resolved here rather than defaulted in the signature: it takes a `git` in
    # the clone, and the clone is what the lines above just established.
    base_branch = base_branch or resolve_base_branch(target, runner)

    # An empty repository has no commits on any branch, so origin/<base_branch>
    # cannot exist. Probe before checkout rather than dying on raw git fatal output.
    ref_check = runner(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base_branch}"],
        cwd=str(target),
        check=False,
    )
    if getattr(ref_check, "returncode", 1) != 0:
        all_commits = runner(
            ["git", "rev-list", "-n", "1", "--all"],
            cwd=str(target),
            check=False,
        )
        if (
            getattr(all_commits, "returncode", 1) != 0
            or not (getattr(all_commits, "stdout", "") or "").strip()
        ):
            raise GitOpsRepoEmpty(
                f"{repo} has no commits on any branch; the audit cannot open a remediation branch"
            )
        raise RuntimeError(f"{repo} has no remote branch origin/{base_branch}")

    runner(["git", "reset", "--hard", "--quiet"], cwd=str(target), check=False)
    runner(["git", "clean", "-fdq"], cwd=str(target), check=False)
    runner(
        ["git", "checkout", "-B", base_branch, f"origin/{base_branch}"],
        cwd=str(target),
    )
    return target


def ensure_scratch_workspace(
    repo: str,
    *,
    lease: str,
    root: str | Path | None = None,
    reset: bool = False,
    owner: str | None = None,
) -> Path:
    """The same leased path as `ensure_workspace`, with no clone in it.

    What a caller gets is a directory and nothing else: no `.git`, no remote, no
    checkout. It is for the content-passing path, where the repository lives in
    the broker and the agent's side of the exchange is a pile of files it wrote.
    Removing the clone is the point — a `.git` the agent can write is where a
    filter driver, an alias or a hook path would have to be defined for the
    known code-execution routes through the credential container to work, and
    an agent that never has one cannot define any of them.

    Everything else about the lease is unchanged, deliberately. The path is the
    same function of repository and lease, so `start` and `finish` still find
    each other with no lookup state; the marker is still written, so the reaper
    still collects the directory when the stream stops running; and the reap
    still happens here, so a fleet that migrates does not leave its old clones
    behind forever.

    `reset` empties the directory. Same rule as the clone path: only the command
    that runs *before* the agent writes anything may ask for it.
    """
    root = Path(root if root is not None else default_root())
    lease = sanitize_lease(lease)
    holder = lease_dir(root, lease)
    target = workspace_path(repo, root, lease=lease)

    with workspace_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        reap_stale_leases(root, keep={lease})
        write_lease(holder, lease, repo, owner=owner)

    # `reset` is the only thing that deletes, including when what is there is a
    # clone left by the directory path. A stream whose broker was armed between
    # `start` and `finish` finds one, and clearing it then would delete the
    # manifests the agent wrote in between — the same data loss `reset=False`
    # exists to prevent on the clone path. Nothing runs `git` here, so an
    # unwanted `.git` sitting in the directory for one run is inert; the next
    # `start` takes it away.
    if reset and target.exists():
        _remove_tree(target)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _remove_tree(path: Path) -> None:
    """Delete a directory tree without importing shutil's whole surface.

    Scoped deliberately: only ever called on a path this module composed under
    its own root, and only on a lease directory or a destination that is not a
    git working tree.
    """
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


def configure_identity(
    target: Path,
    runner: Runner,
    *,
    name: str | None = None,
    email: str | None = None,
) -> None:
    """Give the clone a committer identity.

    `git commit` fails outright with "Please tell me who you are" when neither
    `user.name` nor an env fallback is set, and the container image sets
    neither. The failure surfaces as a non-zero commit that the caller has to
    tell apart from "nothing staged" — so the cheaper fix is to make it
    impossible. Repository-local, never `--global`: the clone is disposable and
    the agent should not be rewriting a shared gitconfig.
    """
    name = name or os.environ.get("GIT_AUTHOR_NAME") or "Platform Agent"
    email = (
        email
        or os.environ.get("GIT_AUTHOR_EMAIL")
        or "platform-agent@users.noreply.github.com"
    )
    runner(["git", "config", "user.name", name], cwd=str(target))
    runner(["git", "config", "user.email", email], cwd=str(target))


def _valid_repo_component(part: str) -> bool:
    """Reject path components unsafe to hand to `gh -R` or use as a filesystem path.

    The slug pattern permits "." and "-", so it happily produces "../..", and a
    leading dash is parsed by `gh` as a flag. Neither is a shape the regex can
    express.
    """
    return bool(part) and part not in (".", "..") and not part.startswith("-")


def is_valid_repo_slug(repo: str) -> bool:
    """Validate that repo is formatted as owner/name without path traversal or flag injection."""
    if not repo or not BARE_REPO_RE.match(repo):
        return False
    owner, _, name = repo.partition("/")
    return _valid_repo_component(owner) and _valid_repo_component(name)


def extract_github_slug(entry: str) -> str | None:
    """Extracts 'owner/repo' slug from a raw URL or shorthand if it refers to GitHub."""
    s = entry.strip()
    if not s:
        return None
    if s.endswith(".git"):
        s = s[:-4]
    s = s.rstrip("/")

    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:", "github.com/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    else:
        if "://" in s or (":" in s and "@" in s):
            return None

    s = s.lstrip("/")
    if is_valid_repo_slug(s):
        return s
    return None


DEFAULT_GITOPS_STATE_PATH = "/etc/gitops/managed_repos"
GITOPS_STATE_READ_TIMEOUT_SECONDS = 30


def _parse_managed_repos_json(repos_str: str) -> list[dict[str, str]]:
    """Parse JSON string containing list of repo specifications."""
    repos_str = repos_str.strip()
    if not repos_str:
        return []
    entries: list[dict[str, str]] = []
    if repos_str.startswith("["):
        try:
            parsed = json.loads(repos_str)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        url = str(item.get("url", "")).strip()
                        repo_type = str(item.get("type", "")).strip()
                        if url and repo_type:
                            entries.append({"type": repo_type, "url": url})
        except json.JSONDecodeError:
            pass
    return entries


def get_managed_repo_entries() -> list[dict[str, str]]:
    """Reads managed repos from the mounted state file or falls back to ConfigMap via kubectl."""
    state_file_path = os.environ.get("GITOPS_STATE_PATH", DEFAULT_GITOPS_STATE_PATH)
    state_file = Path(state_file_path)
    if state_file.is_file():
        try:
            content = state_file.read_text(encoding="utf-8")
            return _parse_managed_repos_json(content)
        except Exception:
            pass
    elif state_file.parent.is_dir():
        # The mount is there and the key is not, which is what kubelet projects
        # for a ConfigMap with no data: an install with nothing registered. That
        # is a known-empty list, not an unreadable one, and the two get
        # different answers from callers that gate on the list. Falling through
        # to kubectl here would turn it into the unreadable one -- the broker
        # pod has no context of its own to pass, so the fallback fails and every
        # gated call answers 503 instead of refusing the repository.
        return []

    cfg_name = os.environ.get("GITOPS_STATE_CONFIGMAP", "platform-agent-gitops-state")
    ns = os.environ.get("KUBE_DEFAULT_NAMESPACE", "kubeagents-system")
    cmd = ["kubectl", "get", "configmap", cfg_name, "-n", ns, "-o", "json"]
    context = os.environ.get("KUBE_CONTEXT_NAME", "").strip()
    if context:
        cmd.extend(["--context", context])
    cwd = agent_home() if Path(agent_home()).is_dir() else None
    try:
        cm_res = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=True,
            timeout=GITOPS_STATE_READ_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as e:
        raise RuntimeError("kubectl binary not found in PATH") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"Timed out after {GITOPS_STATE_READ_TIMEOUT_SECONDS}s reading ConfigMap "
            f"{cfg_name} in namespace {ns}"
        ) from e
    except subprocess.CalledProcessError as e:
        err_msg = (e.stderr or e.stdout or "").strip()
        raise RuntimeError(
            f"Failed to read ConfigMap {cfg_name} in namespace {ns}: {err_msg} (exit code {e.returncode})"
        ) from e

    try:
        cm = json.loads(cm_res.stdout)
        if not isinstance(cm, dict):
            raise RuntimeError(f"ConfigMap JSON is not an object: {cm}")
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Failed to parse ConfigMap {cfg_name} JSON output: {e}"
        ) from e

    repos_str = (cm.get("data") or {}).get("managed_repos", "")
    return _parse_managed_repos_json(repos_str)


def get_managed_github_repos() -> list[str]:
    """Extracts managed GitHub repositories ('owner/name' slugs) from the state ConfigMap."""
    entries = get_managed_repo_entries()
    res: list[str] = []
    for e in entries:
        if e.get("type") == "github":
            slug = extract_github_slug(e["url"])
            if slug and slug not in res:
                res.append(slug)
    return res


def resolve_repo(workspace: str | Path | None = None) -> str:
    """Resolve the GitOps repository as `owner/name`.

    Order:
    1. Workspace path clone decoding (if a leased workspace directory is provided).
    2. Workspace lease record (fallback if workspace is the lease holder directory).
    3. Git remote origin of workspace (if workspace is provided).
    4. ConfigMap state ($GITOPS_STATE_CONFIGMAP).
    5. Local git remote origin fallback (for local development/inside clone).
    """
    if workspace is not None:
        try:
            workspace_p = Path(workspace).resolve()
            holder = lease_holder(workspace_p)
            if holder is not None:
                try:
                    rel = workspace_p.relative_to(holder.resolve())
                    if rel.parts:
                        clone_segment = rel.parts[0]
                        if "__" in clone_segment:
                            owner, sep, name = clone_segment.partition("__")
                            if owner and name:
                                return f"{owner}/{name}"
                except ValueError:
                    pass
                record = read_lease(holder)
                if record and record.get("repo"):
                    return record["repo"]
        except Exception:
            pass

        try:
            from github_token_refresh import get_current_git_repo

            repo = get_current_git_repo(cwd=str(workspace))
            if repo and "/" in repo:
                return repo
        except Exception:
            pass

    managed = get_managed_github_repos()
    if len(managed) == 1:
        return managed[0]
    elif len(managed) > 1:
        raise RuntimeError(
            f"Multiple repositories configured in ConfigMap ({', '.join(managed)}): "
            "please specify the target repository explicitly (e.g. via --repo <owner/repo>)."
        )

    from github_token_refresh import get_current_git_repo

    repo = get_current_git_repo()
    if not repo or "/" not in repo:
        raise RuntimeError(
            f"Could not resolve the target repository as owner/name: "
            f"no repos in ConfigMap ($GITOPS_STATE_CONFIGMAP), "
            f"and no origin remote in {Path.cwd()}"
        )
    return repo


def run_git(argv: list[str], cwd: str | Path, *, check: bool = True):
    """A plain `git` runner for callers with no logging seam of their own.

    `submit_suggestion.py` uses this; `audit_report.py` injects its own recorded
    runner instead. `cwd` is mandatory rather than defaulted — a git command
    with no stated working directory is the bug this whole module exists to fix.
    """
    return subprocess.run(
        ["git", *argv], cwd=str(cwd), check=check, capture_output=True, text=True
    )
