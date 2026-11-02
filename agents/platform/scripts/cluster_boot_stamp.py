#!/usr/bin/env python3
"""The boot stamp that lets the entrypoint skip a Cluster Agent profile it has already refreshed.

docker-entrypoint.sh's cluster loop (step 2.6, continued) re-syncs every
``profiles/cluster-*`` home from the image on every container start: the
persona files, the skills tree, the shared plugin code, and four targeted
config repairs, about six Python processes per profile. Every one of those
steps is a function of the image alone, so a profile the loop has already
brought up to date for this image has nothing left to take from it. The loop
runs serially, and past a few hundred profiles it outlives the readiness
budget of the pod that is waiting on it.

This script turns that loop into a comparison. ``digest`` hashes, once per
start, everything the loop reads: the cluster template tree, the shared
plugins it copies, the two scaffold scripts and the entrypoint itself. The
loop writes that digest into each home it refreshes without a warning, as
``.scaffold-stamp``, and ``stale`` names the homes whose stamp does not match
the digest, so the next start runs the loop for those alone. A dotfile at the
root of a home is excluded from the sandbox mirror (deploy/shared/sandbox_mirror.py,
``is_excluded``), so the stamp never reaches the shell pod or its inventory.

An input that does not exist is hashed as absent rather than refused: a build
without the ``common`` plugin tree still gets a digest, and one that gains it
changes the digest. A stamp that is missing, unreadable or garbled is stale.
Deleting a home's stamp forces a full pass on the next start.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

# The stamp's file name at the root of a cluster profile home. The entrypoint
# writes it (CLUSTER_BOOT_STAMP_FILE there); the two must agree.
STAMP_FILE = ".scaffold-stamp"
# Prefixes the digest so a change to how it is computed invalidates every
# stamp on the volume at once, rather than reading as current.
DIGEST_VERSION = "v1"
# The profile homes the entrypoint's loop covers.
CLUSTER_PROFILE_GLOB = "cluster-*"
# A name the shell can carry one per line. The sanitiser in
# cluster_agent_profile.profile_name mints nothing outside it; a home whose name
# falls outside cannot be listed safely, and the run says so and exits non-zero,
# which the entrypoint reads as "refresh everything, stamp nothing".
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
# Record separators inside the hash. Nul cannot occur in a path, so a path and
# the bytes after it cannot be confused for another path.
_SEP = b"\0"
_CHUNK_BYTES = 1 << 20
# How an entry is hashed: its kind, its path relative to the input, and for a
# file its executable bit and bytes, for a symlink its target.
_KIND_FILE = b"f"
_KIND_DIR = b"d"
_KIND_LINK = b"l"
_KIND_ABSENT = b"-"
_KIND_OTHER = b"?"
_EXEC_BIT = 0o111

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNLISTABLE = 3


def _feed_file(h: "hashlib._Hash", path: Path) -> None:
    mode = path.stat().st_mode
    h.update(b"x" if mode & _EXEC_BIT else b"-")
    h.update(_SEP)
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)


def _feed_entry(h: "hashlib._Hash", path: Path, rel: str) -> None:
    """One filesystem entry, by kind. Symlinks are hashed as their target, never followed."""
    h.update(rel.encode("utf-8", "surrogateescape"))
    h.update(_SEP)
    if path.is_symlink():
        h.update(_KIND_LINK)
        h.update(_SEP)
        h.update(os.readlink(path).encode("utf-8", "surrogateescape"))
    elif path.is_dir():
        h.update(_KIND_DIR)
    elif path.is_file():
        h.update(_KIND_FILE)
        h.update(_SEP)
        _feed_file(h, path)
    elif path.exists():
        h.update(_KIND_OTHER)
    else:
        h.update(_KIND_ABSENT)
    h.update(_SEP)


def _feed_tree(h: "hashlib._Hash", root: Path) -> None:
    """Every entry under *root*, in a fixed order, each by its path relative to *root*."""
    _feed_entry(h, root, "")
    if root.is_symlink() or not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        base = Path(dirpath)
        for name in sorted(dirnames + filenames):
            path = base / name
            _feed_entry(h, path, str(path.relative_to(root)))
            # os.walk lists a symlinked directory under dirnames and would not
            # descend into it (followlinks=False); it is already hashed as a link.


def digest(inputs: list[str]) -> str:
    """The digest of *inputs* in order: ``<version>-<sha256 hex>``."""
    h = hashlib.sha256()
    h.update(DIGEST_VERSION.encode("ascii"))
    h.update(_SEP)
    for index, raw in enumerate(inputs):
        # The ordinal rather than the path: the paths are fixed image paths, and
        # hashing them would make a test's temporary roots part of the answer.
        h.update(str(index).encode("ascii"))
        h.update(_SEP)
        _feed_tree(h, Path(raw))
    return f"{DIGEST_VERSION}-{h.hexdigest()}"


def read_stamp(home: Path) -> str | None:
    """The digest a home's stamp carries, or None when it has none or it cannot be read."""
    try:
        text = (home / STAMP_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    value = text.strip()
    return value or None


def stale_profiles(profiles_dir: Path, current: str) -> list[str]:
    """The ``cluster-*`` homes under *profiles_dir* whose stamp is not *current*, sorted.

    Raises ValueError naming a home whose name cannot be printed one per line.
    """
    stale = []
    for home in sorted(profiles_dir.glob(CLUSTER_PROFILE_GLOB)):
        if not home.is_dir():
            continue
        if read_stamp(home) == current:
            continue
        if not SAFE_NAME.match(home.name):
            raise ValueError(f"profile home {home.name!r} has a name that cannot be listed one per line")
        stale.append(home.name)
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_digest = sub.add_parser("digest", help="print the digest of the inputs, in order")
    p_digest.add_argument("--input", action="append", default=[], metavar="PATH",
                          help="a file or tree the cluster loop reads; repeatable, order matters")
    p_stale = sub.add_parser("stale", help="print the cluster-* homes whose stamp is not the digest")
    p_stale.add_argument("--profiles-dir", required=True, type=Path)
    p_stale.add_argument("--digest", required=True)
    args = parser.parse_args(argv)
    if args.command == "digest":
        if not args.input:
            parser.error("digest needs at least one --input")
        print(digest(args.input))
        return EXIT_OK
    try:
        names = stale_profiles(args.profiles_dir, args.digest)
    except ValueError as exc:
        print(f"cluster_boot_stamp: {exc}", file=sys.stderr)
        return EXIT_UNLISTABLE
    for name in names:
        print(name)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
