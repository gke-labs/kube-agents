"""Git and filesystem loaders feeding FileChange objects into classify_changes.

Uses only git plumbing already permitted in the broker's VCS_GIT_SUBCOMMANDS
(`diff-tree`, `ls-tree`, `archive`) and reads blobs into memory via tarfile
without ever unpacking repository files to disk.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .classifier import classify_changes
from .parser import MAX_FILE_BYTES, MAX_TOTAL_BYTES
from .types import (
    CHANGE_ADDED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    CHANGE_RENAMED,
    ClassificationResult,
    FileChange,
)

# Max number of changed files read per PR before failing closed as unknown
MAX_CHANGED_FILES = 500
_SPECIAL_MODES = frozenset({"120000", "160000"})
_DISABLE_ATTRS_CONTENT = "* -export-subst -export-ignore -ident -text -working-tree-encoding\n"

GitRunner = Callable[..., subprocess.CompletedProcess]


def _default_git_runner(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    prefix = ("--git-dir=.",) if (cwd / "HEAD").is_file() else ()
    return subprocess.run(
        ["git", *prefix, *args],
        cwd=str(cwd),
        capture_output=True,
        text=False,
        check=check,
    )


def _as_bytes(out: str | bytes) -> bytes:
    if isinstance(out, bytes):
        return out
    return out.encode("utf-8", errors="surrogateescape")


def _parse_diff_tree_raw_z(raw: bytes) -> list[dict[str, str]]:
    """Parse `git diff-tree -r -M -z --raw --no-abbrev <base> <head>` output."""
    if not raw:
        return []
    tokens = raw.split(b"\x00")
    entries: list[dict[str, str]] = []
    i = 0
    n = len(tokens)
    while i < n:
        header = tokens[i]
        if not header:
            i += 1
            continue
        if not header.startswith(b":"):
            # Skip leading commit header if present
            i += 1
            continue
        parts = header[1:].decode("ascii", errors="replace").split(" ")
        if len(parts) < 5:
            raise ValueError(f"malformed diff-tree raw header: {header!r}")
        old_mode, new_mode, old_oid, new_oid, status = parts[0], parts[1], parts[2], parts[3], parts[4]
        code = status[0]
        if code in ("R", "C"):
            if i + 2 >= n:
                raise ValueError("truncated rename/copy entry in diff-tree output")
            old_path = tokens[i + 1].decode("utf-8", errors="replace")
            new_path = tokens[i + 2].decode("utf-8", errors="replace")
            i += 3
            entries.append(
                {
                    "status": CHANGE_RENAMED if code == "R" else CHANGE_ADDED,
                    "old_mode": old_mode,
                    "new_mode": new_mode,
                    "old_oid": old_oid,
                    "new_oid": new_oid,
                    "old_path": old_path if code == "R" else "",
                    "path": new_path,
                }
            )
        else:
            if i + 1 >= n:
                raise ValueError("truncated entry in diff-tree output")
            path = tokens[i + 1].decode("utf-8", errors="replace")
            i += 2
            change_type = {
                "A": CHANGE_ADDED,
                "D": CHANGE_DELETED,
                "M": CHANGE_MODIFIED,
                "T": CHANGE_MODIFIED,
            }.get(code, CHANGE_MODIFIED)
            entries.append(
                {
                    "status": change_type,
                    "old_mode": old_mode,
                    "new_mode": new_mode,
                    "old_oid": old_oid,
                    "new_oid": new_oid,
                    "old_path": "",
                    "path": path,
                }
            )
    return entries


def _parse_ls_tree_sizes(raw: bytes) -> dict[str, int]:
    """Parse `git ls-tree -r -l -z <rev> -- <paths>` -> `{path: size}`."""
    sizes: dict[str, int] = {}
    if not raw:
        return sizes
    for rec in raw.split(b"\x00"):
        if not rec or b"\t" not in rec:
            continue
        meta_b, path_b = rec.split(b"\t", 1)
        meta_parts = meta_b.decode("ascii", errors="replace").split()
        if len(meta_parts) >= 4:
            size_s = meta_parts[3]
            if size_s.isdigit():
                sizes[path_b.decode("utf-8", errors="replace")] = int(size_s)
    return sizes


def _read_blobs_via_archive(
    git: GitRunner,
    repo_dir: Path,
    rev: str,
    paths: list[str],
    scratch_dir: Path,
) -> dict[str, bytes]:
    """Read exact blob bytes for `paths` at `rev` using `git archive --format=tar --output=...`.

    Reads directly from the tar archive in RAM without unpacking any file to disk.
    """
    if not paths:
        return {}
    fd, tmp_name = tempfile.mkstemp(dir=str(scratch_dir), prefix="risk-blobs-", suffix=".tar")
    os.close(fd)
    tar_path = Path(tmp_name)
    blobs: dict[str, bytes] = {}
    try:
        # Chunk paths to avoid OS ARG_MAX limits on large PRs
        chunk_size = 200
        for start in range(0, len(paths), chunk_size):
            chunk = paths[start : start + chunk_size]
            git(
                repo_dir,
                "--literal-pathspecs",
                "archive",
                "--format=tar",
                f"--output={tar_path}",
                rev,
                "--",
                *chunk,
            )
            with tarfile.open(tar_path, "r") as tf:
                for member in tf.getmembers():
                    if not member.isreg():
                        continue
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    raw = extracted.read(MAX_FILE_BYTES + 1)
                    blobs[member.name] = raw
    finally:
        tar_path.unlink(missing_ok=True)
    return blobs


def _looks_binary(data: bytes | None) -> bool:
    if not data:
        return False
    return b"\x00" in data[:8192]


def _validate_rev(rev: str, label: str) -> str:
    """Reject empty, control-char, or option-like git revisions (`--...`)."""
    if not isinstance(rev, str) or not rev or rev.startswith("-"):
        raise ValueError(f"invalid git {label} revision: {rev!r}")
    if any(ord(ch) < 0x20 or ch.isspace() for ch in rev):
        raise ValueError(f"invalid git {label} revision: {rev!r}")
    return rev


def _local_file_mode(p: Path | None) -> str:
    if p is None:
        return ""
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        return "120000"
    if st.st_mode & 0o111:
        return "100755"
    return "100644"


def load_git_changes(
    repo_dir: Path,
    base_rev: str,
    head_rev: str,
    *,
    git: GitRunner | None = None,
    scratch_dir: Path | None = None,
    disable_gitattributes: bool = False,
) -> tuple[list[FileChange], list[str]]:
    """Load `FileChange` items and changed paths between `base_rev` and `head_rev`.

    Works on both bare and non-bare git repositories with zero working-tree checkout.
    When `disable_gitattributes=True` (used on broker scratch bare repos), writes
    `info/attributes` to neutralize `.gitattributes` `export-ignore`/`export-subst`.
    All archived blobs are verified against `ls-tree -r -l -z` sizes so hidden or
    mutated archive entries fail closed even when `disable_gitattributes=False`.
    """
    _validate_rev(base_rev, "base")
    _validate_rev(head_rev, "head")
    runner = git or _default_git_runner
    if disable_gitattributes:
        attrs = (repo_dir / "info" / "attributes") if (repo_dir / "HEAD").is_file() else (repo_dir / ".git" / "info" / "attributes")
        attrs.parent.mkdir(parents=True, exist_ok=True)
        attrs.write_text(_DISABLE_ATTRS_CONTENT, encoding="utf-8")
    diff_proc = runner(
        repo_dir,
        "diff-tree",
        "-r",
        "-M",
        "-z",
        "--raw",
        "--no-abbrev",
        base_rev,
        head_rev,
    )
    entries = _parse_diff_tree_raw_z(_as_bytes(diff_proc.stdout))
    all_paths = sorted({e["path"] for e in entries} | {e["old_path"] for e in entries if e["old_path"]})
    if not entries:
        return [], []
    if len(entries) > MAX_CHANGED_FILES:
        # Keep EVERY changed path so governance/CI/critical path triage is never
        # bypassed by padding a PR past MAX_CHANGED_FILES.
        return [
            FileChange(
                path=e["path"],
                old_path=e["old_path"],
                change_type=e["status"],
                before_mode=e["old_mode"] if e["old_mode"] != "000000" else "",
                after_mode=e["new_mode"] if e["new_mode"] != "000000" else "",
                oversized=True,
            )
            for e in entries
        ], all_paths

    base_paths: list[str] = []
    head_paths: list[str] = []
    for e in entries:
        if e["status"] != CHANGE_ADDED and e["old_mode"] not in _SPECIAL_MODES:
            base_paths.append(e["old_path"] or e["path"])
        if e["status"] != CHANGE_DELETED and e["new_mode"] not in _SPECIAL_MODES:
            head_paths.append(e["path"])

    base_sizes: dict[str, int] = {}
    head_sizes: dict[str, int] = {}
    if base_paths:
        for start in range(0, len(base_paths), 200):
            proc = runner(
                repo_dir,
                "--literal-pathspecs",
                "ls-tree",
                "-r",
                "-l",
                "-z",
                base_rev,
                "--",
                *base_paths[start : start + 200],
            )
            base_sizes.update(_parse_ls_tree_sizes(_as_bytes(proc.stdout)))
    if head_paths:
        for start in range(0, len(head_paths), 200):
            proc = runner(
                repo_dir,
                "--literal-pathspecs",
                "ls-tree",
                "-r",
                "-l",
                "-z",
                head_rev,
                "--",
                *head_paths[start : start + 200],
            )
            head_sizes.update(_parse_ls_tree_sizes(_as_bytes(proc.stdout)))

    # Filter out individually oversized files BEFORE streaming tar blobs
    fetch_base: list[str] = []
    fetch_head: list[str] = []
    running_total = 0
    oversized_paths: set[str] = set()

    for p in sorted(set(base_paths)):
        sz = base_sizes.get(p, 0)
        if sz > MAX_FILE_BYTES or running_total + sz > MAX_TOTAL_BYTES:
            oversized_paths.add(p)
        else:
            running_total += sz
            fetch_base.append(p)
    for p in sorted(set(head_paths)):
        sz = head_sizes.get(p, 0)
        if sz > MAX_FILE_BYTES or running_total + sz > MAX_TOTAL_BYTES:
            oversized_paths.add(p)
        else:
            running_total += sz
            fetch_head.append(p)

    tmp_ctx = None
    work_scratch = scratch_dir
    if work_scratch is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="risk-classifier-")
        work_scratch = Path(tmp_ctx.name)
    try:
        before_blobs = _read_blobs_via_archive(runner, repo_dir, base_rev, fetch_base, work_scratch)
        after_blobs = _read_blobs_via_archive(runner, repo_dir, head_rev, fetch_head, work_scratch)
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()

    fetch_base_set = set(fetch_base)
    fetch_head_set = set(fetch_head)
    changes: list[FileChange] = []
    for e in entries:
        path = e["path"]
        old_path = e["old_path"]
        b_key = old_path or path
        b_raw = before_blobs.get(b_key) if e["status"] != CHANGE_ADDED else None
        a_raw = after_blobs.get(path) if e["status"] != CHANGE_DELETED else None
        is_over = (
            path in oversized_paths
            or b_key in oversized_paths
            or (b_raw is not None and len(b_raw) > MAX_FILE_BYTES)
            or (a_raw is not None and len(a_raw) > MAX_FILE_BYTES)
        )
        integrity_err = ""
        if not is_over:
            if b_key in fetch_base_set:
                if b_raw is None:
                    integrity_err = "git archive omitted base blob (possible .gitattributes export-ignore)"
                elif b_key not in base_sizes or len(b_raw) != base_sizes[b_key]:
                    integrity_err = (
                        "git archive base blob size disagreed with ls-tree "
                        "(possible .gitattributes export-subst or filter)"
                    )
            if not integrity_err and path in fetch_head_set:
                if a_raw is None:
                    integrity_err = "git archive omitted head blob (possible .gitattributes export-ignore)"
                elif path not in head_sizes or len(a_raw) != head_sizes[path]:
                    integrity_err = (
                        "git archive head blob size disagreed with ls-tree "
                        "(possible .gitattributes export-subst or filter)"
                    )
        is_bin = _looks_binary(b_raw) or _looks_binary(a_raw)
        changes.append(
            FileChange(
                path=path,
                old_path=old_path,
                change_type=e["status"],
                before=None if (is_over or is_bin or integrity_err) else b_raw,
                after=None if (is_over or is_bin or integrity_err) else a_raw,
                before_mode=e["old_mode"] if e["old_mode"] != "000000" else "",
                after_mode=e["new_mode"] if e["new_mode"] != "000000" else "",
                is_binary=is_bin,
                oversized=is_over,
                integrity_error=integrity_err,
            )
        )
    return changes, all_paths


def classify_git_range(
    repo_dir: Path,
    base_rev: str,
    head_rev: str,
    *,
    git: GitRunner | None = None,
    scratch_dir: Path | None = None,
    disable_gitattributes: bool = False,
) -> tuple[ClassificationResult, list[str]]:
    """Classify all changes between `base_rev` and `head_rev` in a local/bare git repo."""
    changes, paths = load_git_changes(
        repo_dir,
        base_rev,
        head_rev,
        git=git,
        scratch_dir=scratch_dir,
        disable_gitattributes=disable_gitattributes,
    )
    return classify_changes(changes), paths


def load_path_pair(before_path: str, after_path: str, *, logical_path: str = "") -> list[FileChange]:
    """Load `FileChange` entries comparing two local files or directories (or `/dev/null`)."""
    def _is_null(p: str) -> bool:
        return not p or p == os.devnull or p == "/dev/null"

    b_null = _is_null(before_path)
    a_null = _is_null(after_path)
    if b_null and a_null:
        return []

    b_p = None if b_null else Path(before_path)
    a_p = None if a_null else Path(after_path)

    if (b_p is not None and b_p.is_dir()) or (a_p is not None and a_p.is_dir()):
        b_files: dict[str, Path] = {}
        a_files: dict[str, Path] = {}
        if b_p is not None and b_p.is_dir():
            for f in sorted(b_p.rglob("*")):
                if f.is_file() or f.is_symlink():
                    b_files[f.relative_to(b_p).as_posix()] = f
        if a_p is not None and a_p.is_dir():
            for f in sorted(a_p.rglob("*")):
                if f.is_file() or f.is_symlink():
                    a_files[f.relative_to(a_p).as_posix()] = f
        out: list[FileChange] = []
        for rel in sorted(set(b_files.keys()) | set(a_files.keys())):
            bf = b_files.get(rel)
            af = a_files.get(rel)
            b_mode = _local_file_mode(bf)
            a_mode = _local_file_mode(af)
            b_bytes = None if (bf is None or b_mode == "120000") else bf.read_bytes()
            a_bytes = None if (af is None or a_mode == "120000") else af.read_bytes()
            if b_bytes == a_bytes and (bf is not None and af is not None) and (b_mode == a_mode):
                continue
            ctype = CHANGE_ADDED if bf is None else (CHANGE_DELETED if af is None else CHANGE_MODIFIED)
            out.append(
                FileChange(
                    path=rel,
                    change_type=ctype,
                    before=b_bytes,
                    after=a_bytes,
                    before_mode=b_mode,
                    after_mode=a_mode,
                    is_binary=_looks_binary(b_bytes) or _looks_binary(a_bytes),
                    oversized=(len(b_bytes or b"") > MAX_FILE_BYTES or len(a_bytes or b"") > MAX_FILE_BYTES),
                )
            )
        return out

    b_mode = _local_file_mode(b_p)
    a_mode = _local_file_mode(a_p)
    b_bytes = None if (b_p is None or b_mode == "120000") else b_p.read_bytes()
    a_bytes = None if (a_p is None or a_mode == "120000") else a_p.read_bytes()
    rel = logical_path or (a_p.name if a_p is not None else b_p.name)
    ctype = CHANGE_ADDED if b_p is None else (CHANGE_DELETED if a_p is None else CHANGE_MODIFIED)
    return [
        FileChange(
            path=rel,
            change_type=ctype,
            before=b_bytes,
            after=a_bytes,
            before_mode=b_mode,
            after_mode=a_mode,
            is_binary=_looks_binary(b_bytes) or _looks_binary(a_bytes),
            oversized=(len(b_bytes or b"") > MAX_FILE_BYTES or len(a_bytes or b"") > MAX_FILE_BYTES),
        )
    ]
