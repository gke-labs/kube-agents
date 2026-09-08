#!/usr/bin/env python3
"""Read a GitHub repository this pod has no checkout of.

Content-passing took the agent's `.git` away, and with it the ad-hoc `git clone`
that used to be how a question about somebody else's code got answered. The
clone itself was never the point — the point was having the source to read — so
this script gets the source a different way: the broker clones, and the agent
pulls file content out of it over the workspace protocol.

What lands locally is a tree of files with no `.git` in it. That is the whole
difference from the old path, and it is deliberate: a directory with no
repository in it has no `.git/config`, so none of the config-driven ways to make
git execute something exist in it. See docs/designs/agent-shell-sandboxing.md,
"Content-passing removes the shared tree".

Two shapes of use, and they answer different questions.

    inspect_repository.py clone --repo kubernetes-sigs/kustomize
        The whole tree (or a `--prefix` of it) copied into a scratch directory.
        Use it when the analysis is "read this code" and the repository is small
        enough to be worth having on disk.

    inspect_repository.py open --repo kubernetes-sigs/kustomize
    inspect_repository.py grep --handle <h> --pattern 'func NewCmd'
    inspect_repository.py fetch --handle <h> --into ./scratch <paths…>
    inspect_repository.py close --handle <h>
        Search first, then take only the files the search named. Use it on a
        repository too large to copy, which is most of them.

Every subcommand prints one JSON object on stdout.

There are no silent caps anywhere in here. A listing that ended at the broker's
ceiling says `truncated` and the caller pages with a cursor; a file the broker
would not send comes back in `skipped` with the reason. A tool that quietly
returns nine tenths of a repository is worse than one that fails, because the
analysis reads as complete.

On an install whose broker has not been armed for content-passing, `clone` falls
back to a leased checkout on the shared volume — the arrangement that predates
this script — and reports `"mode": "directory"`. The other subcommands need a
broker handle and say so rather than pretending.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

# Append global scripts path to allow importing the shared helpers
sys.path.append("/opt/defaults/scripts")
sys.path.append("/opt/data/scripts")
# The same directory in a source checkout, where nothing is staged into /opt.
sys.path.append(str(Path(__file__).resolve().parents[3] / "scripts"))

import credential_proxy_client
import gitops_workspace
import workspace_paths

OWNER = "inspect-repository"

# Under the broker's own per-request ceilings (256 paths, 8 MiB), with room to
# spare: the broker counts the encoded bytes and this counts the file sizes the
# listing reported, and the two are not the same number.
BATCH_PATHS = 100
BATCH_BYTES = 6 << 20

# Bounds on a `clone`, so a repository nobody sized cannot fill the agent's
# volume. Both are reported when they bite.
DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_BYTES = 64 << 20


def proxy_endpoint() -> str:
    return os.environ.get("CREDENTIAL_PROXY_URL", "").strip()


def content_mode_available() -> bool:
    """Asked of the broker rather than read from a local flag.

    The two mechanisms run side by side during the migration and the switch is
    not in this container: the broker either serves the routes or it does not.
    """
    endpoint = proxy_endpoint()
    if not endpoint:
        return False
    return credential_proxy_client.workspaces_available(endpoint)


def scratch_root() -> Path:
    return Path(gitops_workspace.agent_home()) / "scratch" / "repos"


def default_into(repo: str) -> Path:
    owner, _, name = repo.partition("/")
    return scratch_root() / f"{owner}__{name}"


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


def write_files(into: Path, files: dict[str, bytes]) -> int:
    """Materialise broker content under `into`, refusing any name that escapes it.

    The paths came from the broker and the broker already validated them, so
    this is a second check of the same property against a different filesystem.
    That is on purpose: this is the boundary where a name becomes a write, and
    the check that matters is the one next to the effect.
    """
    written = 0
    for path, data in files.items():
        relative = workspace_paths.validate_path(path)
        target = into / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written += 1
    return written


def open_workspace(args) -> credential_proxy_client.Workspace:
    return credential_proxy_client.Workspace.open(
        proxy_endpoint(),
        args.repo,
        base=getattr(args, "ref", None),
        depth=getattr(args, "depth", None),
    )


def rebind(handle: str) -> credential_proxy_client.Workspace:
    """A client-side Workspace around a handle an earlier turn printed.

    The agent's shell does not survive between turns; the broker's tree does.
    The handle is the whole of the state that has to travel, which is the
    property that makes this protocol usable from a shell at all.
    """
    return credential_proxy_client.Workspace(
        proxy_endpoint(),
        {"handle": handle, "repo": "", "base": "", "baseSha": ""},
    )


def require_content_mode(command: str) -> None:
    if not content_mode_available():
        raise SystemExit(
            f"{command} needs the content-passing broker, which this install "
            "has not armed. Use `clone`, which falls back to a leased checkout "
            "you can read with ordinary tools."
        )


def handle_open(args) -> int:
    require_content_mode("open")
    workspace = open_workspace(args)
    print(
        json.dumps(
            {
                "mode": "content",
                "handle": workspace.handle,
                "repo": workspace.repo,
                "base": workspace.base,
                "shallow": workspace.shallow,
            }
        )
    )
    return 0


def handle_list(args) -> int:
    require_content_mode("list")
    listing = rebind(args.handle).list(args.prefix, after=args.after)
    print(
        json.dumps(
            {
                "entries": list(listing),
                "total": listing.total,
                "truncated": listing.truncated,
                "next": listing[-1]["path"] if listing and listing.truncated else None,
            }
        )
    )
    return 0


def handle_grep(args) -> int:
    require_content_mode("grep")
    print(
        json.dumps(
            rebind(args.handle).grep(
                args.pattern,
                prefix=args.prefix,
                regex=args.regex,
                ignore_case=args.ignore_case,
            )
        )
    )
    return 0


def handle_fetch(args) -> int:
    require_content_mode("fetch")
    if not args.paths:
        raise SystemExit("name at least one repository-relative path to fetch")
    # No empty-directory check, unlike `clone`. Fetching is how a caller builds
    # a directory up over several turns — a search, then the files it named, then
    # the ones those turned out to reference — so a non-empty destination is the
    # normal case rather than the suspicious one.
    into = Path(args.into).expanduser()
    into.mkdir(parents=True, exist_ok=True)
    files, skipped = rebind(args.handle).read_many(args.paths)
    written = write_files(into, files)
    print(json.dumps({"into": str(into), "written": written, "skipped": skipped}))
    return 0


def handle_close(args) -> int:
    """No availability probe. This is the command that releases the clone.

    A handle exists only because an `open` succeeded, so the probe can tell the
    caller nothing it does not already know -- and a probe that fails on a
    broker having a bad few seconds refuses the close, leaving the clone on the
    broker's volume with nothing left holding its handle. Send the close and let
    the broker answer.
    """
    rebind(args.handle).close()
    print(json.dumps({"closed": True}))
    return 0


def clone_content(args) -> int:
    """Page the listing, batch the reads, write the tree, drop the handle."""
    into = prepare_destination(
        Path(args.into) if args.into else default_into(args.repo), args.force
    )
    skipped: list[dict] = []
    written = 0
    total_bytes = 0
    stopped = None
    with open_workspace(args) as workspace:
        cursor: str | None = None
        batch: list[str] = []
        batch_bytes = 0

        def flush() -> None:
            nonlocal batch, batch_bytes, written
            if not batch:
                return
            files, missed = workspace.read_many(batch)
            written += write_files(into, files)
            skipped.extend(missed)
            batch = []
            batch_bytes = 0

        while stopped is None:
            listing = workspace.list(args.prefix, after=cursor)
            if not listing:
                break
            for entry in listing:
                if written + len(batch) >= args.max_files:
                    stopped = "maxFiles"
                    break
                if total_bytes + entry["size"] > args.max_bytes:
                    stopped = "maxBytes"
                    break
                total_bytes += entry["size"]
                batch.append(entry["path"])
                batch_bytes += entry["size"]
                if len(batch) >= BATCH_PATHS or batch_bytes >= BATCH_BYTES:
                    flush()
            flush()
            if not listing.truncated:
                break
            cursor = listing[-1]["path"]
        base = workspace.base
        repo = workspace.repo
    result = {
        "mode": "content",
        "repo": repo,
        "base": base,
        "into": str(into),
        "written": written,
        "bytes": total_bytes,
        "skipped": skipped,
        # `stopped` is the honest name for what a bound did. A caller that gets
        # `maxBytes` back has a partial tree and needs to know before it
        # concludes anything about what the repository does not contain.
        "stopped": stopped,
        "complete": stopped is None and not skipped,
    }
    print(json.dumps(result))
    return 0


def clone_directory(args) -> int:
    """The pre-content-passing path: a leased checkout on the shared volume."""
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
                # Said rather than silently done. `--depth` is a broker-side
                # clone option; this path is `git clone` through the shim and
                # takes the full history, so a caller that asked for a shallow
                # read got something else and should know which.
                "depthIgnored": bool(args.depth),
            }
        )
    )
    return 0


def handle_clone(args) -> int:
    if content_mode_available():
        return clone_content(args)
    return clone_directory(args)


def _runner(cmd: list, *, cwd=None, check: bool = True):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read a GitHub repository through the credential broker."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add_repo(sub):
        sub.add_argument("--repo", required=True, help="owner/name")
        sub.add_argument("--ref", help="branch to read; defaults to the remote's HEAD")
        sub.add_argument(
            "--depth",
            type=int,
            help="shallow clone of this many commits; read-only and much smaller",
        )

    clone = commands.add_parser("clone", help="copy a repository into a scratch tree")
    add_repo(clone)
    clone.add_argument("--into", help="destination directory")
    clone.add_argument("--prefix", help="copy only this subtree")
    clone.add_argument("--force", action="store_true", help="write into a non-empty directory")
    clone.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    clone.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    clone.add_argument("--lease", help="directory mode only: the lease to work under")
    clone.set_defaults(func=handle_clone)

    opener = commands.add_parser("open", help="open a broker-side workspace")
    add_repo(opener)
    opener.set_defaults(func=handle_open)

    lister = commands.add_parser("list", help="one page of tracked paths")
    lister.add_argument("--handle", required=True)
    lister.add_argument("--prefix")
    lister.add_argument("--after", help="the `next` value from the previous page")
    lister.set_defaults(func=handle_list)

    searcher = commands.add_parser("grep", help="search the tracked files")
    searcher.add_argument("--handle", required=True)
    searcher.add_argument("--pattern", required=True)
    searcher.add_argument("--prefix")
    searcher.add_argument("--regex", action="store_true", help="POSIX extended regex")
    searcher.add_argument("--ignore-case", action="store_true")
    searcher.set_defaults(func=handle_grep)

    fetcher = commands.add_parser("fetch", help="copy named files into a directory")
    fetcher.add_argument("--handle", required=True)
    fetcher.add_argument("--into", required=True)
    fetcher.add_argument("paths", nargs="*")
    fetcher.set_defaults(func=handle_fetch)

    closer = commands.add_parser("close", help="drop the broker-side tree")
    closer.add_argument("--handle", required=True)
    closer.set_defaults(func=handle_close)

    return parser


def dispatch(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def main() -> int:
    """Every way a broker call can fail, as a sentence and an exit code.

    The two workspace exceptions are the broker answering. The other two are it
    not answering at all: `TokenUnavailable` is the projected ServiceAccount
    token missing or empty, which `_workspace_call` raises before it opens a
    socket, and a bare `URLError` is the connection itself -- the broker Pod
    down, the Service not resolving. Both used to reach the terminal as
    tracebacks, which reads to the agent as this script being broken rather than
    the broker being unreachable, and sends it to fix the wrong thing.
    """
    try:
        return dispatch(sys.argv[1:])
    except credential_proxy_client.WorkspaceUnavailable as exc:
        print(f"the broker has no content workspaces: {exc}", file=sys.stderr)
        return 1
    except credential_proxy_client.WorkspaceRequestError as exc:
        print(json.dumps({"error": str(exc), "status": exc.status}), file=sys.stderr)
        return 1
    except credential_proxy_client.TokenUnavailable as exc:
        print(f"no credential to call the broker with: {exc}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"the broker is unreachable: {exc.reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
