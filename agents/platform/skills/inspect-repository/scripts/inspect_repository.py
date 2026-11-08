#!/usr/bin/env python3
"""Read a repository this pod has no checkout of.

Content-passing took the agent's `.git` away, and with it the ad-hoc `git clone`
that used to be how a question about somebody else's code got answered. The
clone itself was never the point — the point was having the source to read — so
this script gets the source a different way: the broker clones one revision and
sends its files back as a tar (`/v1/vcs/snapshot`, `vcs_client.snapshot`).

What lands locally is a tree of files with no `.git` in it. That is the whole
difference from the old path, and it is deliberate: a directory with no
repository in it has no `.git/config`, so none of the config-driven ways to make
git execute something exist in it. See docs/designs/agent-shell-sandboxing.md,
"Content-passing removes the shared tree".

    inspect_repository.py clone --repo kubernetes-sigs/kustomize
        One revision's tree (or a `--prefix` of it) copied into a scratch
        directory. A repository over the snapshot cap is refused whole; name a
        `--prefix` to take the part that matters.

The command prints one JSON object on stdout.

There are no silent caps anywhere in here. A copy over a bound is refused, not
cut short, and a symbolic link that would point out of the tree comes back in
`skipped` with the reason. A tool that quietly returns nine tenths of a
repository is worse than one that fails, because the analysis reads as complete.

On an install with no broker, `clone` falls back to a leased checkout on the
shared volume — the arrangement that predates this script — and reports
`"mode": "directory"`. A broker older than the snapshot verb is an error, not a
fallback: the sandbox's `git` has no transport, so that checkout cannot work
there, and the broker's answer says which image to roll forward.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Append global scripts path to allow importing the shared helpers
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
# The same directory in a source checkout, where nothing is staged into /opt.
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

import gitops_workspace
import vcs_client

OWNER = "inspect-repository"

# Bounds on a `clone`, so a repository nobody sized cannot fill the agent's
# volume. A copy past either one is refused whole, and the refusal says which.
DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_BYTES = vcs_client.SNAPSHOT_MAX_UNPACKED_BYTES

# The broker's refusal of a `paths` entry that names nothing at the revision.
PATH_NOT_FOUND = "PATH_NOT_FOUND"

# The folder under the scratch directory that holds every clone's staging, and
# nothing else. The script owns it, so a sweep of it removes no file of the
# caller's.
STAGING_DIR_NAME = ".inspect-repository-staging"
# The name of one clone's staging directory, before mkdtemp's suffix.
STAGING_PREFIX = "clone."

# How old an entry in the staging folder must be before a clone removes it. A
# clone takes seconds, so a younger one can belong to a clone that runs now.
# The same age as sandbox_artifact_patch.STALE_SWEEP_AGE_SECONDS.
STALE_STAGING_AGE_SECONDS = 3600.0


def scratch_root() -> Path:
    return Path(gitops_workspace.agent_home()) / "scratch" / "repos"


def default_into(repo: str) -> Path:
    """Where a clone of `repo` lands when the caller names no directory.

    GitHub's `owner/name` keeps the `owner__name` it always had. A name on
    another forge carries its host and nests deeper, so every `/` is spelled
    `%2F`: one directory, and no two names land in the same one.
    """
    if repo.count("/") == 1:
        owner, _, name = repo.partition("/")
        return scratch_root() / f"{owner}__{name}"
    return scratch_root() / repo.replace("/", "%2F")


def staging_root() -> Path:
    """The folder that holds every clone's staging, beside `scratch_root()`."""
    return scratch_root().parent / STAGING_DIR_NAME


def prepare_destination(into: Path, force: bool) -> Path:
    """An empty directory to write into, or a refusal naming `--force`.

    Writing into a directory that already holds files is how one analysis reads
    another's leftovers as part of the repository it asked for. Refusing is the
    only answer that does not either lose the caller's data or lie to them.
    """
    into = Path(into).expanduser()
    if into.exists() and any(into.iterdir()):
        if not force:
            raise SystemExit(
                f"{into} is not empty. Pass --force to write into it anyway, or "
                "name an empty directory with --into."
            )
    into.mkdir(parents=True, exist_ok=True)
    return into


def remove_stale_staging() -> None:
    """Remove the staging directories that an earlier clone left.

    A clone removes its staging directory in a `finally`, which a SIGKILL does
    not run. Every staging directory is in `staging_root()`, so each entry
    there that is older than STALE_STAGING_AGE_SECONDS is removed, whatever
    its name. Nothing outside that folder is removed. A missing folder or a
    failure here does not stop the clone.
    """
    cutoff = time.time() - STALE_STAGING_AGE_SECONDS
    try:
        entries = list(staging_root().iterdir())
    except OSError:
        return
    for path in entries:
        try:
            # `rmtree` refuses a symbolic link, so a link is never followed.
            if path.is_dir() and path.lstat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


def copy_over(staging: Path, into: Path) -> None:
    """Copy a checked snapshot over `into`: a file or a link gives way, a directory merges.

    `--force` adds a second `--prefix` to a tree, and the two can overlap.
    `copytree` cannot replace a symbolic link that is already there, and it
    writes a file or a directory through one. So a file or a link that the
    snapshot names again is removed first. A directory is never removed: the
    repository being read chooses its names, and a file it calls `notes` must
    not cost the caller a `notes/` directory. A directory and a file under one
    name is refused, and every name is checked before anything is removed, so
    a refusal leaves `into` as it was.
    """
    replace = []
    for root, dirs, files in os.walk(staging):
        for name in dirs + files:
            new = Path(root) / name
            old = into / new.relative_to(staging)
            new_is_dir = new.is_dir() and not new.is_symlink()
            if old.is_symlink() or not old.exists():
                if old.is_symlink():
                    replace.append(old)
                if new_is_dir:
                    # Nothing of `into` lies below: a link goes, so the walk
                    # must not follow it to whatever it names.
                    dirs.remove(name)
                continue
            if old.is_dir() != new_is_dir:
                kind = "directory" if old.is_dir() else "file"
                raise SystemExit(
                    f"{old} is a {kind}, and the copy holds a "
                    f"{'directory' if new_is_dir else 'file'} of that name. "
                    "Nothing was written. Name an empty directory with --into."
                )
            if not new_is_dir:
                replace.append(old)
    for old in replace:
        old.unlink()
    shutil.copytree(staging, into, symlinks=True, dirs_exist_ok=True)


# The reasons the old copy gave under `skipped`. The fleet audit tells a file
# it may have missed from a path that is not a file by them
# (audit_report.BROKER_SKIP_NOT_A_FILE_REASONS), and every path the snapshot
# skips is one of these two: a symbolic link, or a member of a directory laid
# out as a bare repository.
SKIPPED_LINK = "symlink"
SKIPPED_NOT_A_FILE = "notAFile"


def skipped_reason(reason: str) -> str:
    """The old reply's word for one of the snapshot's `skipped` reasons."""
    return SKIPPED_NOT_A_FILE if "bare" in reason else SKIPPED_LINK


def clone_content(args) -> int:
    """Take one snapshot into a fresh directory, check it, then copy it into place.

    The snapshot unpacks only into an empty directory, and `--force` is how a
    caller adds a second `--prefix` to a tree it is building (the fleet audit
    does). So every clone stages in `staging_root()`, and the copy goes into
    the directory that `into` names. `into` itself is never removed or
    replaced. So a second clone that writes into it at the same time does not
    fail on it, and an `into` that is the current directory, a symbolic link
    or a mount point stays valid. The snapshot stages beside its own
    destination, which is the staging directory here, so its staging is in
    `staging_root()` too.
    """
    remove_stale_staging()
    target = Path(args.into).expanduser() if args.into else default_into(args.repo)
    # The sweep removes what sits in staging_root(), so a copy there would be
    # removed an hour later.
    resolved, root_resolved = target.resolve(), staging_root().resolve()
    if resolved == root_resolved or root_resolved in resolved.parents:
        raise SystemExit(
            f"{target} is in {staging_root()}, which holds only clone staging. "
            "Name a directory outside it with --into."
        )
    into = prepare_destination(target, args.force)
    paths = [args.prefix] if args.prefix else None
    root = staging_root()
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=str(root), prefix=STAGING_PREFIX))
    try:
        try:
            copy = vcs_client.snapshot(
                args.repo, args.ref, paths, into=staging,
                max_bytes=args.max_bytes, max_files=args.max_files,
            )
        except vcs_client.VcsError as exc:
            if exc.code == vcs_client.SNAPSHOT_TOO_MANY_FILES:
                raise SystemExit(
                    f"{args.repo}: {exc} (--max-files). Nothing was written. "
                    "Name a --prefix to take part of the tree."
                ) from exc
            if exc.code != PATH_NOT_FOUND or not paths:
                raise
            # The prefix names nothing at this revision: an empty copy of it,
            # as a listing of the prefix would have been. The refusal carries
            # the revision the broker read, so the caller still has the commit.
            print(json.dumps({
                "mode": "content", "forge": None, "repo": args.repo, "role": None,
                "base": args.ref, "sha": exc.revision or "", "into": str(into),
                "written": 0, "bytes": 0, "skipped": [], "stopped": None,
                "complete": True, "history": "none", "missing": args.prefix,
            }))
            return 0
        copy_over(staging, into)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(json.dumps({
        "mode": "content",
        "forge": copy["forge"],
        "repo": copy["repo"],
        "role": copy["role"],
        "base": copy["ref"],
        # The commit the copy was taken from. A caller that has to say which
        # commit it read — the fleet-audit declared-intent record names each
        # repository as `owner/name@sha` — takes it from here, since there is
        # no `.git` on this side to ask.
        "sha": copy["revision"],
        "into": str(into),
        "written": copy["files"],
        "bytes": copy["bytes"],
        "skipped": [
            {"path": entry["path"], "reason": skipped_reason(entry["reason"])}
            for entry in copy["skipped"]
        ],
        # A bound refuses the whole copy, so nothing is ever cut short. Kept so
        # that a caller that reads the key sees no bound bit.
        "stopped": None,
        "complete": not copy["skipped"],
        # One revision, always: `--depth` changes nothing in this mode.
        "history": copy["history"],
    }))
    return 0


def clone_directory(args) -> int:
    """The leased-checkout path: a checkout on the shared volume."""
    repo = args.repo
    lease = gitops_workspace.lease_id(args.lease)
    workspace = gitops_workspace.ensure_workspace(
        repo,
        _runner,
        lease=lease,
        base_branch=args.ref,
        reset=True,
        owner=OWNER,
    )
    print(
        json.dumps(
            {
                "mode": "directory",
                "repo": repo,
                "workspace": str(workspace),
                "lease": lease,
                "complete": True,
                # Said rather than silently done. This path is a plain local
                # `git clone`, which takes the full history, so a caller that
                # asked for a shallow read got something else and should know
                # which. In the sandbox that `git` has no transport, so this
                # fallback cannot clone there at all.
                "depthIgnored": bool(args.depth),
            }
        )
    )
    return 0


def handle_clone(args) -> int:
    if not os.environ.get(vcs_client.BROKER_ENDPOINT_VAR, "").strip():
        return clone_directory(args)
    return clone_content(args)


def _runner(cmd: list, *, cwd=None, check: bool = True):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read a repository through the credential broker."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    clone = commands.add_parser("clone", help="copy one revision of a repository into a scratch tree")
    clone.add_argument(
        "--repo", required=True,
        help="owner/name on GitHub, or <host>/<path> on another forge this install serves",
    )
    clone.add_argument("--ref", help="branch or tag to read; defaults to the base the broker pins the repository to, else the remote's HEAD")
    clone.add_argument(
        "--depth",
        type=int,
        help="accepted for older callers; a content copy is always one revision",
    )
    clone.add_argument("--into", help="destination directory")
    clone.add_argument("--prefix", help="copy only this directory or file, a path rather than a glob")
    clone.add_argument("--force", action="store_true", help="write into a non-empty directory")
    clone.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    clone.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    clone.add_argument("--lease", help="directory mode only: the lease to work under")
    clone.set_defaults(func=handle_clone)

    return parser


def dispatch(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def main() -> int:
    """Every way a broker call can fail, as JSON on stderr and an exit code.

    `vcs_client` turns each failure -- the broker refusing, the token missing,
    the connection down -- into one `VcsError`. A traceback reads to the agent
    as this script being broken rather than the broker answering, and sends it
    to fix the wrong thing.
    """
    try:
        return dispatch(sys.argv[1:])
    except vcs_client.VcsError as exc:
        answer = exc.as_json()
        if exc.code == vcs_client.SNAPSHOT_TOO_LARGE:
            answer["hint"] = "Name a --prefix to take part of the tree."
        print(json.dumps(answer), file=sys.stderr)
        return 1
    except OSError as exc:
        # The copy into `--into` failed part way: a name taken by something
        # the snapshot cannot replace, or a full disk.
        print(json.dumps({"error": f"the copy into the destination failed: {exc}"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
