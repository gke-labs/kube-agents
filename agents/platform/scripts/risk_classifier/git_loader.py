"""Git and filesystem loaders feeding FileChange objects into classify_changes.

Uses only git plumbing already permitted in the broker's VCS_GIT_SUBCOMMANDS
(`diff-tree`, `ls-tree`, `archive`) and reads blobs into memory via tarfile
without ever unpacking repository files to disk.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

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

# Max number of changed files whose blobs are loaded per PR before marking excess oversized
MAX_CHANGED_FILES = 500
MAX_SIZED_CANDIDATES = 2_000
MAX_LOCAL_DIR_ENTRIES = 10_000
DEFAULT_GIT_TIMEOUT_SECONDS = 30.0

_SPECIAL_MODES = frozenset({"120000", "160000"})
_MANIFEST_SUFFIXES = (".yaml", ".yml", ".json")
_DISABLE_ATTRS_CONTENT = "* -export-subst -export-ignore -ident -text -working-tree-encoding\n"
_HEX_SHA_CHARS = frozenset("0123456789abcdefABCDEF")

GitRunner = Callable[..., subprocess.CompletedProcess]


def _default_git_runner(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    is_bare = (cwd / "HEAD").is_file() and not (cwd / ".git").exists()
    prefix = ("--git-dir=.",) if is_bare else ()
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        return subprocess.run(
            ["git", *prefix, *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=False,
            check=check,
            timeout=DEFAULT_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"git command timed out after {exc.timeout}s") from exc


def _run_git(runner: GitRunner, cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = runner(cwd, *args, check=check)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"git command timed out after {exc.timeout}s") from exc
    if getattr(proc, "truncated", False):
        sub = next((tok for tok in args if not tok.startswith("-")), "git")
        raise ValueError(f"git {sub} output was truncated at ceiling")
    return proc


def _as_bytes(out: str | bytes) -> bytes:
    if isinstance(out, bytes):
        return out
    return out.encode("utf-8", errors="surrogateescape")


def _decode_git_path(raw_path: bytes) -> tuple[str, bool]:
    """Decode a git tree path strictly; return `(path_str, is_clean_utf8_without_controls)`."""
    if not raw_path:
        raise ValueError("empty file path in git diff-tree output")
    try:
        decoded = raw_path.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return raw_path.decode("utf-8", errors="replace"), False
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in decoded):
        return decoded, False
    return decoded, True


def _is_manifest_path(path: str) -> bool:
    return path.lower().endswith(_MANIFEST_SUFFIXES)


def _parse_diff_tree_raw_z(raw: bytes) -> list[dict[str, object]]:
    """Parse `git diff-tree -r -M -z --raw --no-abbrev <base> <head> --` output."""
    if not raw:
        return []
    tokens = raw.split(b"\x00")
    entries: list[dict[str, object]] = []
    i = 0
    n = len(tokens)
    while i < n:
        header = tokens[i]
        if not header:
            i += 1
            continue
        if not header.startswith(b":"):
            # Skip leading hex commit OID header if emitted by a single-tree invocation
            hdr_str = header.decode("ascii", errors="replace")
            if len(hdr_str) in (40, 64) and all(c in _HEX_SHA_CHARS for c in hdr_str):
                i += 1
                continue
            raise ValueError(f"unexpected diff-tree record header: {header!r}")
        parts = header[1:].decode("ascii", errors="replace").split(" ")
        if len(parts) < 5:
            raise ValueError(f"malformed diff-tree raw header: {header!r}")
        old_mode, new_mode, old_oid, new_oid, status = parts[0], parts[1], parts[2], parts[3], parts[4]
        if not status:
            raise ValueError(f"missing status in diff-tree header: {header!r}")
        code = status[0]
        if code in ("U", "X"):
            raise ValueError(f"unmerged or unknown git diff-tree status {status!r}")
        if code in ("R", "C"):
            if i + 2 >= n:
                raise ValueError("truncated rename/copy entry in diff-tree output")
            old_path, old_ok = _decode_git_path(tokens[i + 1])
            new_path, new_ok = _decode_git_path(tokens[i + 2])
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
                    "path_valid": (old_ok and new_ok) if code == "R" else new_ok,
                }
            )
        else:
            if i + 1 >= n:
                raise ValueError("truncated entry in diff-tree output")
            path, path_ok = _decode_git_path(tokens[i + 1])
            i += 2
            change_type = {
                "A": CHANGE_ADDED,
                "D": CHANGE_DELETED,
                "M": CHANGE_MODIFIED,
                "T": CHANGE_MODIFIED,
                "B": CHANGE_MODIFIED,
            }.get(code)
            if change_type is None:
                raise ValueError(f"unsupported git diff-tree status {status!r}")
            entries.append(
                {
                    "status": change_type,
                    "old_mode": old_mode,
                    "new_mode": new_mode,
                    "old_oid": old_oid,
                    "new_oid": new_oid,
                    "old_path": "",
                    "path": path,
                    "path_valid": path_ok,
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
                try:
                    decoded = path_b.decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    continue
                sizes[decoded] = int(size_s)
    return sizes


def _read_blobs_via_archive(
    git: GitRunner,
    repo_dir: Path,
    rev: str,
    paths: list[str],
    scratch_dir: Path,
    *,
    expected_sizes: Mapping[str, int],
    remaining_budget: list[int],
) -> tuple[dict[str, bytes], set[str], set[str]]:
    """Read exact blob bytes for `paths` at `rev` using `git archive --format=tar --output=...`.

    Reads directly from the tar archive in RAM without unpacking any file to disk,
    skipping symlinks/hardlinks/non-regular entries and bounding cumulative RAM.
    Returns `(blobs, size_mismatch_paths, archive_oversized_paths)`.
    """
    if not paths:
        return {}, set(), set()
    fd, tmp_name = tempfile.mkstemp(dir=str(scratch_dir), prefix="risk-blobs-", suffix=".tar")
    os.close(fd)
    tar_path = Path(tmp_name)
    blobs: dict[str, bytes] = {}
    mismatch_paths: set[str] = set()
    archive_oversized: set[str] = set()
    seen_members: set[str] = set()
    try:
        chunk_size = 200
        for start in range(0, len(paths), chunk_size):
            chunk = [p for p in paths[start : start + chunk_size] if p]
            if not chunk:
                continue
            _run_git(
                git,
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
                    if member.issym() or member.islnk() or not member.isreg():
                        continue
                    name = member.name
                    if name in seen_members:
                        # Duplicate tar member for the same path -> fail integrity closed
                        mismatch_paths.add(name)
                        blobs.pop(name, None)
                        continue
                    seen_members.add(name)
                    exp_sz = expected_sizes.get(name)
                    if exp_sz is not None and member.size != exp_sz:
                        mismatch_paths.add(name)
                        continue
                    if member.size > MAX_FILE_BYTES or member.size > remaining_budget[0]:
                        archive_oversized.add(name)
                        continue
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        continue
                    read_cap = min(MAX_FILE_BYTES + 1, remaining_budget[0] + 1)
                    raw = extracted.read(read_cap)
                    if exp_sz is not None and len(raw) != exp_sz:
                        mismatch_paths.add(name)
                        continue
                    if len(raw) > MAX_FILE_BYTES or len(raw) > remaining_budget[0]:
                        archive_oversized.add(name)
                        continue
                    remaining_budget[0] -= len(raw)
                    blobs[name] = raw
    finally:
        tar_path.unlink(missing_ok=True)
    return blobs, mismatch_paths, archive_oversized


def _looks_binary(data: bytes | None) -> bool:
    if not data:
        return False
    return b"\x00" in data[:8192]


def _validate_rev(rev: str, label: str) -> str:
    """Reject empty, control-char, range (`..`), reflog (`@{`), or option-like git revisions."""
    if not isinstance(rev, str) or not rev:
        raise ValueError(f"invalid git {label} revision: {rev!r}")
    if rev.startswith(("-", "^", ":", "+")) or ".." in rev or "@{" in rev:
        raise ValueError(f"invalid git {label} revision: {rev!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch.isspace() for ch in rev):
        raise ValueError(f"invalid git {label} revision: {rev!r}")
    return rev


def _resolve_git_common_dir(repo_dir: Path) -> Path | None:
    """Resolve the git directory holding `info/attributes` for bare, normal, or worktree repos."""
    dot_git = repo_dir / ".git"
    if dot_git.is_symlink():
        raise ValueError(f"refusing symlinked .git path at {dot_git}")
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        content = dot_git.read_text(encoding="utf-8", errors="strict").strip()
        if not content.startswith("gitdir:"):
            raise ValueError(f"malformed .git worktree file at {dot_git}")
        gitdir_raw = content[len("gitdir:") :].strip()
        if not gitdir_raw:
            raise ValueError(f"empty gitdir pointer in {dot_git}")
        git_dir = (repo_dir / gitdir_raw).resolve()
        commondir_file = git_dir / "commondir"
        if commondir_file.is_symlink():
            raise ValueError(f"refusing symlinked commondir at {commondir_file}")
        if commondir_file.is_file():
            common_rel = commondir_file.read_text(encoding="utf-8", errors="strict").strip()
            if common_rel:
                git_dir = (git_dir / common_rel).resolve()
        return git_dir
    if (repo_dir / "HEAD").is_file():
        return repo_dir
    return None


@contextlib.contextmanager
def _temporary_disable_gitattributes(repo_dir: Path, *, enabled: bool) -> Iterator[None]:
    """Temporarily install `info/attributes` neutralization and restore prior state on exit."""
    if not enabled:
        yield
        return
    git_dir = _resolve_git_common_dir(repo_dir)
    if git_dir is None or not git_dir.is_dir():
        yield
        return
    info_dir = git_dir / "info"
    if info_dir.is_symlink():
        raise ValueError(f"refusing symlinked git info directory at {info_dir}")
    attrs_path = info_dir / "attributes"
    if attrs_path.is_symlink():
        raise ValueError(f"refusing symlinked git info/attributes file at {attrs_path}")

    info_existed = info_dir.exists()
    prior_bytes: bytes | None = attrs_path.read_bytes() if attrs_path.is_file() else None
    info_dir.mkdir(parents=True, exist_ok=True)
    disable_bytes = _DISABLE_ATTRS_CONTENT.encode("utf-8")
    if prior_bytes:
        sep = b"" if prior_bytes.endswith(b"\n") else b"\n"
        new_bytes = prior_bytes + sep + disable_bytes
    else:
        new_bytes = disable_bytes
    attrs_path.write_bytes(new_bytes)
    try:
        yield
    finally:
        try:
            if prior_bytes is None:
                attrs_path.unlink(missing_ok=True)
                if not info_existed:
                    try:
                        info_dir.rmdir()
                    except OSError:
                        pass
            else:
                attrs_path.write_bytes(prior_bytes)
        except OSError:
            pass


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

    Works on bare, standard, and worktree git repositories with zero working-tree checkout.
    When `disable_gitattributes=True`, temporarily overrides `info/attributes` to
    neutralize `.gitattributes` `export-ignore`/`export-subst` and restores prior state.
    All archived blobs are verified against `ls-tree -r -l -z` sizes and budgeted with
    `.yaml`/`.yml`/`.json` manifests prioritized smallest-first so padding files can
    never starve a small critical manifest or push memory past `MAX_TOTAL_BYTES`.
    """
    _validate_rev(base_rev, "base")
    _validate_rev(head_rev, "head")
    runner = git or _default_git_runner

    with _temporary_disable_gitattributes(repo_dir, enabled=disable_gitattributes):
        diff_proc = _run_git(
            runner,
            repo_dir,
            "diff-tree",
            "-r",
            "-M",
            "-z",
            "--raw",
            "--no-abbrev",
            base_rev,
            head_rev,
            "--",
        )
        entries = _parse_diff_tree_raw_z(_as_bytes(diff_proc.stdout))
        all_paths = sorted(
            {str(e["path"]) for e in entries} | {str(e["old_path"]) for e in entries if e["old_path"]}
        )
        if not entries:
            return [], []

        # Partition entries needing blob reads: prioritize `.yaml`/`.yml`/`.json` paths first
        # so padding a PR with >500 non-manifest files can never hide a manifest change.
        candidate_indices: list[int] = []
        oversized_indices: set[int] = set()
        for idx, e in enumerate(entries):
            if not e["path_valid"]:
                continue
            need_b = e["status"] != CHANGE_ADDED and str(e["old_mode"]) not in _SPECIAL_MODES
            need_a = e["status"] != CHANGE_DELETED and str(e["new_mode"]) not in _SPECIAL_MODES
            if need_b or need_a:
                candidate_indices.append(idx)

        candidate_indices.sort(
            key=lambda idx: (
                0
                if (
                    _is_manifest_path(str(entries[idx]["path"]))
                    or (bool(entries[idx]["old_path"]) and _is_manifest_path(str(entries[idx]["old_path"])))
                )
                else 1,
                str(entries[idx]["path"]),
                str(entries[idx]["old_path"]),
            )
        )
        if len(candidate_indices) > MAX_SIZED_CANDIDATES:
            for idx in candidate_indices[MAX_SIZED_CANDIDATES:]:
                oversized_indices.add(idx)
            candidate_indices = candidate_indices[:MAX_SIZED_CANDIDATES]

        base_query_paths: list[str] = []
        head_query_paths: list[str] = []
        for idx in candidate_indices:
            e = entries[idx]
            path = str(e["path"])
            old_path = str(e["old_path"])
            if e["status"] != CHANGE_ADDED and str(e["old_mode"]) not in _SPECIAL_MODES:
                base_query_paths.append(old_path or path)
            if e["status"] != CHANGE_DELETED and str(e["new_mode"]) not in _SPECIAL_MODES:
                head_query_paths.append(path)

        unique_base_query = sorted(set(base_query_paths))
        unique_head_query = sorted(set(head_query_paths))
        base_sizes: dict[str, int] = {}
        head_sizes: dict[str, int] = {}
        for start in range(0, len(unique_base_query), 200):
            proc = _run_git(
                runner,
                repo_dir,
                "--literal-pathspecs",
                "ls-tree",
                "-r",
                "-l",
                "-z",
                base_rev,
                "--",
                *unique_base_query[start : start + 200],
            )
            base_sizes.update(_parse_ls_tree_sizes(_as_bytes(proc.stdout)))
        for start in range(0, len(unique_head_query), 200):
            proc = _run_git(
                runner,
                repo_dir,
                "--literal-pathspecs",
                "ls-tree",
                "-r",
                "-l",
                "-z",
                head_rev,
                "--",
                *unique_head_query[start : start + 200],
            )
            head_sizes.update(_parse_ls_tree_sizes(_as_bytes(proc.stdout)))

        # Allocate `MAX_CHANGED_FILES` and `MAX_TOTAL_BYTES` prioritizing manifests smallest-first
        def _entry_priority(idx: int) -> tuple[int, int, str, str]:
            e = entries[idx]
            path = str(e["path"])
            old_path = str(e["old_path"])
            b_key = old_path or path
            is_man = _is_manifest_path(path) or (bool(old_path) and _is_manifest_path(old_path))
            b_sz = base_sizes.get(b_key, 0) if (e["status"] != CHANGE_ADDED and str(e["old_mode"]) not in _SPECIAL_MODES) else 0
            a_sz = head_sizes.get(path, 0) if (e["status"] != CHANGE_DELETED and str(e["new_mode"]) not in _SPECIAL_MODES) else 0
            return (0 if is_man else 1, b_sz + a_sz, path, old_path)

        fetch_base_set: set[str] = set()
        fetch_head_set: set[str] = set()
        running_total = 0
        selected_count = 0

        for idx in sorted(candidate_indices, key=_entry_priority):
            e = entries[idx]
            path = str(e["path"])
            old_path = str(e["old_path"])
            b_key = old_path or path
            need_b = e["status"] != CHANGE_ADDED and str(e["old_mode"]) not in _SPECIAL_MODES
            need_a = e["status"] != CHANGE_DELETED and str(e["new_mode"]) not in _SPECIAL_MODES
            b_sz = base_sizes.get(b_key, 0) if need_b else 0
            a_sz = head_sizes.get(path, 0) if need_a else 0

            if b_sz > MAX_FILE_BYTES or a_sz > MAX_FILE_BYTES or selected_count >= MAX_CHANGED_FILES:
                oversized_indices.add(idx)
                continue

            inc_b = b_sz if (need_b and b_key not in fetch_base_set) else 0
            inc_a = a_sz if (need_a and path not in fetch_head_set) else 0
            if running_total + inc_b + inc_a > MAX_TOTAL_BYTES:
                oversized_indices.add(idx)
                continue

            running_total += inc_b + inc_a
            selected_count += 1
            if need_b:
                fetch_base_set.add(b_key)
            if need_a:
                fetch_head_set.add(path)

        fetch_base = sorted(fetch_base_set)
        fetch_head = sorted(fetch_head_set)

        tmp_ctx = None
        work_scratch = scratch_dir
        if work_scratch is None:
            tmp_ctx = tempfile.TemporaryDirectory(prefix="risk-classifier-")
            work_scratch = Path(tmp_ctx.name)
        remaining_budget = [MAX_TOTAL_BYTES]
        try:
            before_blobs, base_mismatch, base_arch_over = _read_blobs_via_archive(
                runner,
                repo_dir,
                base_rev,
                fetch_base,
                work_scratch,
                expected_sizes=base_sizes,
                remaining_budget=remaining_budget,
            )
            after_blobs, head_mismatch, head_arch_over = _read_blobs_via_archive(
                runner,
                repo_dir,
                head_rev,
                fetch_head,
                work_scratch,
                expected_sizes=head_sizes,
                remaining_budget=remaining_budget,
            )
        finally:
            if tmp_ctx is not None:
                tmp_ctx.cleanup()

    changes: list[FileChange] = []
    for idx, e in enumerate(entries):
        path = str(e["path"])
        old_path = str(e["old_path"])
        status = str(e["status"])
        old_mode = str(e["old_mode"])
        new_mode = str(e["new_mode"])
        b_key = old_path or path

        if not e["path_valid"]:
            changes.append(
                FileChange(
                    path=path,
                    old_path=old_path,
                    change_type=status,
                    before=None,
                    after=None,
                    before_mode=old_mode if old_mode != "000000" else "",
                    after_mode=new_mode if new_mode != "000000" else "",
                    integrity_error="git path contains non-UTF-8 bytes or control characters",
                )
            )
            continue

        b_raw = before_blobs.get(b_key) if status != CHANGE_ADDED else None
        a_raw = after_blobs.get(path) if status != CHANGE_DELETED else None

        integrity_err = ""
        if b_key in base_mismatch:
            integrity_err = (
                "git archive base blob size disagreed with ls-tree "
                "(possible .gitattributes export-subst or filter)"
            )
        elif path in head_mismatch:
            integrity_err = (
                "git archive head blob size disagreed with ls-tree "
                "(possible .gitattributes export-subst or filter)"
            )

        is_over = (
            idx in oversized_indices
            or b_key in base_arch_over
            or path in head_arch_over
            or (b_raw is not None and len(b_raw) > MAX_FILE_BYTES)
            or (a_raw is not None and len(a_raw) > MAX_FILE_BYTES)
        )
        if not is_over and not integrity_err:
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
                change_type=status,
                before=None if (is_over or is_bin or integrity_err) else b_raw,
                after=None if (is_over or is_bin or integrity_err) else a_raw,
                before_mode=old_mode if old_mode != "000000" else "",
                after_mode=new_mode if new_mode != "000000" else "",
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


def _inspect_local_entry(p: Path | None) -> tuple[str, int, str, str]:
    """Inspect `p` via `lstat` without opening or following symlinks.

    Returns `(git_mode, st_size, symlink_target, integrity_error)`.
    """
    if p is None:
        return "", 0, "", ""
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        return "120000", 0, os.readlink(p), ""
    if stat.S_ISREG(st.st_mode):
        mode = "100755" if (st.st_mode & 0o111) else "100644"
        return mode, st.st_size, "", ""
    return (
        "100644",
        0,
        "",
        f"unsupported special file type (mode {oct(stat.S_IFMT(st.st_mode))}) cannot be read safely",
    )


def _open_regular_nofollow(p: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(p, flags)
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise OSError(f"refusing non-regular file at {p}")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_bounded_regular_file(p: Path, max_bytes: int) -> tuple[bytes | None, bool, str]:
    """Read at most `max_bytes` from regular file `p` without following symlinks or blocking on FIFOs."""
    try:
        fd = _open_regular_nofollow(p)
    except OSError as exc:
        return None, False, f"could not safely open file: {exc}"
    with os.fdopen(fd, "rb") as fp:
        raw = fp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return None, True, ""
    return raw, False, ""


def _regular_files_identical_streamed(p1: Path, p2: Path, sz1: int, sz2: int) -> bool:
    """Compare two oversized regular files in 64 KiB chunks using constant O(1) memory."""
    if sz1 != sz2:
        return False
    try:
        fd1 = _open_regular_nofollow(p1)
    except OSError:
        return False
    try:
        fd2 = _open_regular_nofollow(p2)
    except OSError:
        os.close(fd1)
        return False
    with os.fdopen(fd1, "rb") as f1, os.fdopen(fd2, "rb") as f2:
        while True:
            b1 = f1.read(65536)
            b2 = f2.read(65536)
            if b1 != b2:
                return False
            if not b1:
                return True


def _scan_local_directory(root: Path) -> dict[str, Path]:
    """Collect relative POSIX paths under `root` without following directory symlinks or `.git`."""
    found: dict[str, Path] = {}
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        with os.scandir(current) as it:
            entries = sorted(it, key=lambda d: d.name)
        for d in reversed(entries):
            if d.name.lower() == ".git":
                continue
            full = Path(d.path)
            if d.is_symlink():
                rel = full.relative_to(root).as_posix()
                found[rel] = full
            elif d.is_dir(follow_symlinks=False):
                stack.append(full)
            else:
                rel = full.relative_to(root).as_posix()
                found[rel] = full
            if len(found) > MAX_LOCAL_DIR_ENTRIES:
                raise ValueError(f"directory {root} exceeds maximum of {MAX_LOCAL_DIR_ENTRIES} entries")
    return found


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

    b_st = os.lstat(b_p) if b_p is not None else None
    a_st = os.lstat(a_p) if a_p is not None else None
    b_is_dir = b_st is not None and stat.S_ISDIR(b_st.st_mode)
    a_is_dir = a_st is not None and stat.S_ISDIR(a_st.st_mode)

    if b_is_dir or a_is_dir:
        if (b_p is not None and not b_is_dir) or (a_p is not None and not a_is_dir):
            raise ValueError("cannot compare a directory with a non-directory path")
        b_files = _scan_local_directory(b_p) if (b_p is not None and b_is_dir) else {}
        a_files = _scan_local_directory(a_p) if (a_p is not None and a_is_dir) else {}
        prefix = logical_path.strip("/")

        raw_items: list[dict[str, object]] = []
        for rel in sorted(set(b_files.keys()) | set(a_files.keys())):
            bf = b_files.get(rel)
            af = a_files.get(rel)
            b_mode, b_sz, b_lnk, b_err = _inspect_local_entry(bf)
            a_mode, a_sz, a_lnk, a_err = _inspect_local_entry(af)
            err = b_err or a_err

            if bf is not None and af is not None and not err and b_mode == a_mode:
                if b_mode == "120000":
                    if b_lnk == a_lnk:
                        continue
                elif b_sz > MAX_FILE_BYTES or a_sz > MAX_FILE_BYTES:
                    if _regular_files_identical_streamed(bf, af, b_sz, a_sz):
                        continue

            full_rel = f"{prefix}/{rel}" if prefix else rel
            ctype = CHANGE_ADDED if bf is None else (CHANGE_DELETED if af is None else CHANGE_MODIFIED)
            raw_items.append(
                {
                    "path": full_rel,
                    "ctype": ctype,
                    "bf": bf,
                    "af": af,
                    "b_mode": b_mode,
                    "a_mode": a_mode,
                    "b_sz": b_sz,
                    "a_sz": a_sz,
                    "err": err,
                }
            )

        # Allocate `MAX_CHANGED_FILES` and `MAX_TOTAL_BYTES` prioritizing manifests smallest-first
        order = sorted(
            range(len(raw_items)),
            key=lambda i: (
                0 if _is_manifest_path(str(raw_items[i]["path"])) else 1,
                int(raw_items[i]["b_sz"]) + int(raw_items[i]["a_sz"]),
                str(raw_items[i]["path"]),
            ),
        )
        remaining_budget = MAX_TOTAL_BYTES
        loaded_count = 0
        resolved: dict[int, FileChange | None] = {}

        for idx in order:
            it = raw_items[idx]
            path = str(it["path"])
            ctype = str(it["ctype"])
            bf = it["bf"] if isinstance(it["bf"], Path) else None
            af = it["af"] if isinstance(it["af"], Path) else None
            b_mode = str(it["b_mode"])
            a_mode = str(it["a_mode"])
            b_sz = int(it["b_sz"])
            a_sz = int(it["a_sz"])
            err = str(it["err"])

            if err or b_mode == "120000" or a_mode == "120000":
                resolved[idx] = FileChange(
                    path=path,
                    change_type=ctype,
                    before=None,
                    after=None,
                    before_mode=b_mode,
                    after_mode=a_mode,
                    integrity_error=err,
                )
                continue

            if (
                b_sz > MAX_FILE_BYTES
                or a_sz > MAX_FILE_BYTES
                or loaded_count >= MAX_CHANGED_FILES
                or b_sz + a_sz > remaining_budget
            ):
                resolved[idx] = FileChange(
                    path=path,
                    change_type=ctype,
                    before=None,
                    after=None,
                    before_mode=b_mode,
                    after_mode=a_mode,
                    oversized=True,
                )
                continue

            b_bytes: bytes | None = None
            a_bytes: bytes | None = None
            b_over = False
            a_over = False
            if bf is not None:
                b_bytes, b_over, b_open_err = _read_bounded_regular_file(bf, min(MAX_FILE_BYTES, remaining_budget))
                if b_open_err:
                    err = b_open_err
            if not err and not b_over and af is not None:
                rem_after_b = remaining_budget - len(b_bytes or b"")
                a_bytes, a_over, a_open_err = _read_bounded_regular_file(af, min(MAX_FILE_BYTES, rem_after_b))
                if a_open_err:
                    err = a_open_err

            if (
                not err
                and not b_over
                and not a_over
                and bf is not None
                and af is not None
                and b_bytes == a_bytes
                and b_mode == a_mode
            ):
                resolved[idx] = None
                continue

            is_over = b_over or a_over
            is_bin = False if (is_over or err) else (_looks_binary(b_bytes) or _looks_binary(a_bytes))
            if not is_over and not is_bin and not err:
                remaining_budget -= len(b_bytes or b"") + len(a_bytes or b"")
                loaded_count += 1
            resolved[idx] = FileChange(
                path=path,
                change_type=ctype,
                before=None if (is_over or is_bin or err) else b_bytes,
                after=None if (is_over or is_bin or err) else a_bytes,
                before_mode=b_mode,
                after_mode=a_mode,
                is_binary=is_bin,
                oversized=is_over,
                integrity_error=err,
            )

        return [resolved[i] for i in range(len(raw_items)) if resolved.get(i) is not None]  # type: ignore[misc]

    b_mode, b_sz, _, b_err = _inspect_local_entry(b_p)
    a_mode, a_sz, _, a_err = _inspect_local_entry(a_p)
    err = b_err or a_err
    is_over = b_sz > MAX_FILE_BYTES or a_sz > MAX_FILE_BYTES
    b_bytes = None
    a_bytes = None

    if not err and not is_over:
        if b_p is not None and b_mode != "120000":
            b_bytes, b_over, b_open_err = _read_bounded_regular_file(b_p, MAX_FILE_BYTES)
            if b_open_err:
                err = b_open_err
            elif b_over:
                is_over = True
        if not err and not is_over and a_p is not None and a_mode != "120000":
            rem = MAX_TOTAL_BYTES - len(b_bytes or b"")
            a_bytes, a_over, a_open_err = _read_bounded_regular_file(a_p, min(MAX_FILE_BYTES, rem))
            if a_open_err:
                err = a_open_err
            elif a_over:
                is_over = True

    is_bin = False if (is_over or err) else (_looks_binary(b_bytes) or _looks_binary(a_bytes))
    rel = logical_path or (a_p.name if a_p is not None else (b_p.name if b_p is not None else ""))
    ctype = CHANGE_ADDED if b_p is None else (CHANGE_DELETED if a_p is None else CHANGE_MODIFIED)
    return [
        FileChange(
            path=rel,
            change_type=ctype,
            before=None if (is_over or is_bin or err) else b_bytes,
            after=None if (is_over or is_bin or err) else a_bytes,
            before_mode=b_mode,
            after_mode=a_mode,
            is_binary=is_bin,
            oversized=is_over,
            integrity_error=err,
        )
    ]
