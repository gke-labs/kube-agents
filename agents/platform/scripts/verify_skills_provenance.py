#!/usr/bin/env python3
# verify_skills_provenance.py - Check a baked skill tree against the SHA-256
# manifest the image build wrote into it.
#
# The skill trees are prompt material: a SKILL.md tells the agent what to do and
# a skills/*/scripts/*.py runs with the agent's own credentials. Nothing today
# would notice if one of them changed between the build that produced it and the
# boot that loads it, which is the gap this closes — the entrypoint runs this
# over each image tree before step 2 copies any of it onto the PVC, so a tree
# that no longer matches its manifest never reaches a profile.
#
# Scope, stated precisely because it is easy to read more into a checksum than
# it carries:
#   - This verifies the three IMAGE trees (/opt/hermes/skills and the two
#     templates). It deliberately does not verify the per-profile copies on the
#     PVC. Those are legitimately written after the copy — profile_scaffold.py
#     overlays them and the hourly cluster-agent reconcile re-overlays them — so
#     a manifest comparison there reports normal operation as tampering.
#   - It is detection, at boot, of a tree that does not match its build. It is
#     not a runtime tamper barrier: the barrier is that the image trees are
#     root-owned while the agent runs as uid 10000 (see the ownership block in
#     deploy/docker/Dockerfile). This check is what catches the cases ownership
#     cannot — a corrupted layer, a bad build, or a future code path that runs
#     as root.
#   - Every file under the tree is compared. The single exception is the
#     manifest itself, which cannot contain its own checksum; that exclusion is
#     by NAME and applies at any depth, so a plain file called
#     skills_manifest.sha256 planted in a subdirectory is invisible to the build
#     and to this check alike. A symlink so named is not — scan_tree tests
#     is_symlink() before it tests the name. Nothing else is carved out,
#     including compiled bytecode: the Dockerfile runs its
#     compileall pass before it writes the manifest, so __pycache__ is already
#     there to be hashed. A .pyc is not a derivative that comes along for free
#     with its source — CPython's default invalidation is source mtime plus
#     size, not a content hash, so bytecode edited under a preserved mtime is
#     what the interpreter runs. An exclusion list would also be a list of names
#     an attacker can choose: anything matched by one is invisible here, symlink
#     included.
#
# Usage:
#     verify_skills_provenance.py --manifest <path> --dir <tree>
#
# Exits 0 when the tree matches, 1 with the specific difference on stderr when
# it does not.

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

# The name the Dockerfile gives the manifest. Held here as well as there because
# the manifest usually lives INSIDE the tree it describes, so both sides have to
# agree to leave it out of its own checksums.
MANIFEST_NAME = "skills_manifest.sha256"

_READ_CHUNK = 65536


def make_log(prefix: str):
    """Build a stderr logger tagged with a component prefix.

    Byte-identical to profile_scaffold.make_log in this same directory, and
    duplicated rather than imported on purpose: this module runs at entrypoint
    step 1.55, before anything else in the directory has been exercised, and an
    import is one more way for the check to fail to run at all. Its whole
    dependency surface is the standard library.
    """

    def _log(msg: str) -> None:
        print(f"[{prefix}] {msg}", file=sys.stderr)

    return _log


log = make_log("SKILL-PROVENANCE")


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def parse_manifest(manifest_path: Path) -> Dict[str, str]:
    """Read `sha256sum` output into {relative path: digest}.

    The format is the one `sha256sum` writes and `sha256sum -c` reads: a hex
    digest, whitespace, then the path. A leading `*` (its binary-mode marker)
    and a leading `./` (from `find .`) are both stripped so the keys match what
    this script derives from walking the tree.
    """
    entries: Dict[str, str] = {}
    with manifest_path.open("r", encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                raise ValueError(f"malformed line {number} in {manifest_path}: {line!r}")
            digest, name = parts[0].strip().lower(), parts[1].strip()
            if name.startswith("*"):
                name = name[1:]
            if name.startswith("./"):
                name = name[2:]
            entries[name] = digest
    return entries


def scan_tree(directory: Path, manifest_path: Path) -> Tuple[Dict[str, Path], List[str]]:
    """Every covered file under `directory`, plus any symlink found in it.

    Symlinks are separated out rather than followed. The build records the tree
    with `find -type f`, which tests the link itself and so never writes one to
    the manifest, and it refuses outright to build an image whose skill trees
    contain one — meaning a symlink here is always something the build did not
    produce. Hashing through it would not catch that: point one at a file whose
    current content matches and the digest is identical, while what the tree
    actually loads has become a path the manifest says nothing about and that
    can change afterwards without touching anything covered.

    Nothing is skipped by name on the way there. The symlink test is the first
    thing every entry meets, so a link cannot hide behind a filename that some
    earlier branch would have passed over.
    """
    found: Dict[str, Path] = {}
    symlinks: List[str] = []
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        # Checked before is_dir()/is_file(), both of which resolve the link.
        if path.is_symlink():
            symlinks.append(relative.as_posix())
            continue
        if path.is_dir():
            continue
        if relative.name == MANIFEST_NAME or path.resolve() == manifest_path.resolve():
            continue
        found[relative.as_posix()] = path
    return found, symlinks


def verify_provenance(manifest_path: Path, directory: Path) -> Tuple[List[str], int]:
    """Compare `directory` against `manifest_path`; return (differences, files checked).

    An empty difference list means the tree matches. Every difference is
    collected rather than raised at the first one, so a boot that fails reports
    the whole picture instead of one file at a time across successive
    crash-loops.
    """
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest not found at {manifest_path}")
    if not directory.is_dir():
        raise NotADirectoryError(f"skills directory not found at {directory}")

    expected = parse_manifest(manifest_path)
    found, symlinks = scan_tree(directory, manifest_path)

    problems: List[str] = []
    seen: Set[str] = set()

    for name in sorted(symlinks):
        problems.append(f"symlink the build did not produce: {name}")

    for name in sorted(found):
        seen.add(name)
        if name not in expected:
            problems.append(f"untracked file not present at build time: {name}")
            continue
        actual = compute_sha256(found[name])
        if actual != expected[name]:
            problems.append(
                f"content changed since build: {name} (manifest {expected[name]}, found {actual})"
            )

    for name in sorted(set(expected) - seen):
        problems.append(f"file recorded at build time is missing: {name}")

    return problems, len(found)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a baked skill tree against its build-time SHA-256 manifest."
    )
    parser.add_argument("--manifest", required=True, help="Path to the SHA-256 manifest.")
    parser.add_argument("--dir", required=True, help="Path to the skill tree to verify.")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    directory = Path(args.dir)

    try:
        problems, checked = verify_provenance(manifest_path, directory)
    except Exception as exc:  # noqa: BLE001 - the caller wants the reason, not the traceback
        log(f"could not verify {directory}: {exc}")
        return 1

    if problems:
        log(f"{directory} does not match {manifest_path}:")
        for problem in problems:
            log(f"  - {problem}")
        return 1

    log(f"verified {checked} files under {directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
