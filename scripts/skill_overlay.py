#!/usr/bin/env python3
"""Keeps this repository's changes to the mirrored gke-* skills through upstream syncs.

docs/designs/upstream-skill-overlays.md is the design. Each mirrored skill has three layers:

  1. third_party/google-skills/<skill>/        exact copy of google/skills at a pinned commit
  2. agents/platform/skill-overlays/<skill>/    upstream.lock, NNNN-<slug>.patch files, append.md
  3. agents/platform/skills/<skill>/            the generated skill: 1 with 2 applied, committed

A skill is mirrored when its overlay holds an upstream.lock. Skills without one are this
repository's own and are never touched.

Subcommands:
  sync <skill> [--ref R]     move one skill to upstream's latest (or R), rebasing its patches;
                             adopts an upstream skill that is not mirrored yet
  continue <skill>           resume a sync that stopped on a conflict
  import <skill> --ref R     start mirroring a skill this repository already ships: write the
                             upstream copy at R and its lock, leave the generated skill as it is
  refresh <skill>            record edits to the generated skill as a patch
  generate <skill>           rebuild the generated skill from the upstream copy and overlay
  check [<skill>...]         verify copies against their locks and generated skills against
                             copy + overlay, offline
  status                     list skills upstream has moved past, and upstream skills not mirrored
  verify-upstream            compare each copy with google/skills at its locked commit

Requires git 2.34 or newer (rename-aware rebase).
"""

import argparse
import difflib
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_URL_ENV = "SKILL_OVERLAY_UPSTREAM"
DEFAULT_UPSTREAM_URL = "https://github.com/google/skills.git"
UPSTREAM_BRANCH = "main"
UPSTREAM_SKILLS_PATH = "skills/cloud"
# `status` lists upstream skills with this prefix that are not mirrored yet. What counts as
# mirrored is decided by the lock files, never by this prefix.
UPSTREAM_SKILL_PREFIX = "gke-"
COPY_ROOT = REPO_ROOT / "third_party" / "google-skills"
OVERLAY_ROOT = REPO_ROOT / "agents" / "platform" / "skill-overlays"
SKILLS_ROOT = REPO_ROOT / "agents" / "platform" / "skills"
SCRATCH_ROOT = REPO_ROOT / ".skill-sync"
UPSTREAM_CACHE = SCRATCH_ROOT / "upstream.git"
REFRESH_SCRATCH = SCRATCH_ROOT / "refresh"
PARTIAL_CLONE_FILTER = "--filter=blob:none"
LOCK_NAME = "upstream.lock"
LOCK_COMMIT_KEY = "commit"
LOCK_SHA256_KEY = "sha256"
# OS and editor files that are never part of a skill: hashing or comparing them would report a
# clean copy as edited by hand.
JUNK_FILE_PATTERNS = (".DS_Store", "Thumbs.db", "*.swp", "*.swo", "*~")
APPEND_NAME = "append.md"
SKILL_MD = "SKILL.md"
# Every append.md starts with a line that begins with this; the rest of the line is free text.
# The footers scripts/sync-upstream-skills.py appends today carry such a line, so they move into
# append.md byte for byte.
APPEND_MARKER_PREFIX = "<!-- kube-agents: local addition"
PATCH_GLOB = "[0-9][0-9][0-9][0-9]-*.patch"
PATCH_NUMBER_WIDTH = 4
PATCH_SUBJECT_RE = re.compile(rf"\d{{{PATCH_NUMBER_WIDTH}}}-.*\.patch")
SKILL_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
LOCK_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
LOCK_SHA256_RE = re.compile(r"[0-9a-f]{64}")
SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")
SLUG_MAX_LENGTH = 48
DEFAULT_SLUG = "change"
HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? ")
BLAME_LINE_RE = re.compile(r"^[0-9a-f]{40} ")
DIFF_SECTION_PREFIX = "diff --git "
# Where a patch's headers end: its first diff line, in the git form or the traditional form git
# apply also accepts (`--- a/` or `--- /dev/null`). Anchored to a line start and to those exact
# forms, so header prose that quotes them mid-line, or a line that merely begins `--- `, is no cut.
PATCH_DIFF_START_RE = re.compile(r"^(?:diff --git |--- (?:a/|/dev/null))", re.MULTILINE)
NUL = "\0"
DIFF_OLD_FILE_PREFIX = "--- a/"
INDEX_LINE_PREFIX = "index "
BINARY_PATCH_MARKER = "GIT binary patch"
# Lines a conflicted file still holds when its conflict was not resolved. `=======` alone is
# left out: it is also a Markdown heading underline.
CONFLICT_MARKER_PREFIXES = ("<<<<<<< ", ">>>>>>> ")
WHY_PREFIX = "Why:"
STATE_FILE = "sync-state.json"
SERIES_BRANCH = "patches"
BASE_TAG = "base"
NEW_UPSTREAM_BRANCH = "upstream-new"
NEW_COPY_SNAPSHOT = "new-copy"
FIXUP_PREFIX = "fixup! "
UPSTREAM_COPY_COMMIT_MESSAGE = "upstream copy"
EXECUTABLE_MODE = "755"
REGULAR_MODE = "644"
TEXT_ENCODING = "utf-8"
TEXT_ERRORS = "surrogateescape"
PERCENT = 100
EXIT_FAILURE = 1
EXIT_CONFLICT = 2
PATCH_HEADER_TEMPLATE = (
    "Subject: {subject}\n"
    "\n"
    "Why: TODO, say why this repository needs the change\n"
    "Local-Issue: TODO\n"
    "Upstream-Issue: none\n"
    "Retire-When: TODO\n"
    "\n"
)
DEFAULT_SUBJECT = "TODO describe the change"
# Variables that point git at a particular repository, index or config. Inherited from a git
# hook or a wrapper, they would send the scratch repositories' commits into the caller's repo.
REPO_LOCAL_GIT_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_IMPLICIT_WORK_TREE", "GIT_PREFIX",
    "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_GRAFT_FILE",
    "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
)
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    # No user ignore file either: a scratch repository must record every file it is given.
    # and paths printed verbatim, so the diff text the tool parses names a non-ASCII file as is.
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "core.excludesFile",
    "GIT_CONFIG_VALUE_0": os.devnull,
    "GIT_CONFIG_KEY_1": "core.quotePath",
    "GIT_CONFIG_VALUE_1": "false",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "skill-overlay",
    "GIT_AUTHOR_EMAIL": "skill-overlay@example.invalid",
    "GIT_COMMITTER_NAME": "skill-overlay",
    "GIT_COMMITTER_EMAIL": "skill-overlay@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}
NON_INTERACTIVE_EDITORS = {"GIT_SEQUENCE_EDITOR": "true", "GIT_EDITOR": "true"}


class OverlayError(Exception):
    """A user-facing failure; the message says what to do."""


# ---------------------------------------------------------------- helpers


def git(args, cwd, check=True, extra_env=None, input_text=None):
    env = {k: v for k, v in os.environ.items() if k not in REPO_LOCAL_GIT_ENV}
    env.update(GIT_ENV)
    env.update(extra_env or {})
    res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True,
                         input=None if input_text is None else input_text.encode())
    if check and res.returncode != 0:
        raise OverlayError(f"git {' '.join(args)} failed:\n{decode(res.stderr)}")
    return res


def git_out(args, cwd, **kw):
    return decode(git(args, cwd, **kw).stdout).strip()


def git_diff_text(args, cwd):
    """Diff output exactly as git wrote it: trimming would drop trailing blank context lines."""
    return decode(git(args, cwd).stdout)


def decode(data):
    return data.decode(TEXT_ENCODING, TEXT_ERRORS)


def read_exact(path):
    """A file's text with its line endings untouched (no universal-newline translation)."""
    return decode(Path(path).read_bytes())


def write_exact(path, text):
    Path(path).write_bytes(text.encode(TEXT_ENCODING, TEXT_ERRORS))


def upstream_url():
    return os.environ.get(UPSTREAM_URL_ENV, DEFAULT_UPSTREAM_URL)


def copy_dir(skill):
    return COPY_ROOT / skill


def overlay_dir(skill):
    return OVERLAY_ROOT / skill


def generated_dir(skill):
    return SKILLS_ROOT / skill


def rel(path):
    return Path(path).relative_to(REPO_ROOT).as_posix()


def is_mirrored(skill):
    return (overlay_dir(skill) / LOCK_NAME).is_file()


def mirrored_skills():
    if not OVERLAY_ROOT.is_dir():
        return []
    return sorted(p.name for p in OVERLAY_ROOT.iterdir() if (p / LOCK_NAME).is_file())


def read_lock(skill):
    values = {}
    for line in read_exact(overlay_dir(skill) / LOCK_NAME).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            values[key.strip()] = value.strip()
    if not (LOCK_COMMIT_RE.fullmatch(values.get(LOCK_COMMIT_KEY, ""))
            and LOCK_SHA256_RE.fullmatch(values.get(LOCK_SHA256_KEY, ""))):
        raise OverlayError(f"{skill}: {rel(overlay_dir(skill) / LOCK_NAME)} must hold "
                           f"`{LOCK_COMMIT_KEY}: <40-hex commit>` and `{LOCK_SHA256_KEY}: <64-hex digest>`")
    return values


def write_lock(skill, commit, digest):
    overlay_dir(skill).mkdir(parents=True, exist_ok=True)
    write_exact(overlay_dir(skill) / LOCK_NAME, f"{LOCK_COMMIT_KEY}: {commit}\n{LOCK_SHA256_KEY}: {digest}\n")


def files_of(root):
    root = Path(root)
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(root).parts
                  and not any(fnmatch.fnmatchcase(p.name, pattern) for pattern in JUNK_FILE_PATTERNS))


def committable_files(root):
    """The files under root that git would commit in this repository: tracked or untracked but
    not ignored. Editor and OS junk in a skill directory is not part of the skill."""
    res = git(["ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", str(root)],
              cwd=REPO_ROOT, check=False)
    if res.returncode != 0:
        return files_of(root)
    names = []
    for name in decode(res.stdout).split("\0"):
        if name and (REPO_ROOT / name).is_file():
            names.append((REPO_ROOT / name).relative_to(root).as_posix())
    return sorted(set(names))


def file_mode(path):
    # The mode bit git records; os.access() answers no for every file on a noexec mount.
    return EXECUTABLE_MODE if Path(path).stat().st_mode & stat.S_IXUSR else REGULAR_MODE


def tree_sha256(root):
    """sha256 over every file's path, executable bit and bytes, in path order."""
    digest = hashlib.sha256()
    for name in files_of(root):
        path = Path(root) / name
        digest.update(name.encode() + b"\0" + file_mode(path).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def compare_trees(expected, actual):
    """Return a list of 'path: reason' strings where the two trees differ."""
    problems = []
    exp, act = set(files_of(expected)), set(files_of(actual)) if Path(actual).exists() else set()
    for name in sorted(exp - act):
        problems.append(f"{name}: missing")
    for name in sorted(act - exp):
        problems.append(f"{name}: not produced by the upstream copy and overlay")
    for name in sorted(exp & act):
        a, b = Path(expected) / name, Path(actual) / name
        if a.read_bytes() != b.read_bytes():
            problems.append(f"{name}: content differs")
        elif file_mode(a) != file_mode(b):
            problems.append(f"{name}: executable bit differs")
    return problems


def replace_tree(src, dest):
    dest = Path(dest)
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))


def copy_files(src, dest, names):
    for name in names:
        target = Path(dest) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(src) / name, target)


def clear_worktree(repo):
    for child in Path(repo).iterdir():
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()


def patches(skill):
    return sorted(overlay_dir(skill).glob(PATCH_GLOB))


def patch_header(text):
    match = PATCH_DIFF_START_RE.search(text)
    return text if match is None else text[: match.start()]


def patch_why(path):
    for line in read_exact(path).splitlines():
        if line.startswith(WHY_PREFIX):
            return line[len(WHY_PREFIX):].strip()
    return ""


def separator_for(text):
    return "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")


def strip_index_lines(diff):
    """Drop `index` lines from text sections so a refresh only moves hunk headers and lines.

    A binary section keeps its full index line: `git apply` needs it to apply the binary hunk.
    """
    sections, current = [], []
    for line in diff.splitlines(keepends=True):
        if line.startswith(DIFF_SECTION_PREFIX) and current:
            sections.append(current)
            current = []
        current.append(line)
    if current:
        sections.append(current)
    out = []
    for section in sections:
        binary = any(line.startswith(BINARY_PATCH_MARKER) for line in section)
        out.extend(line for line in section if binary or not line.startswith(INDEX_LINE_PREFIX))
    return "".join(out)


def slugify(text):
    slug = SLUG_STRIP_RE.sub("-", text.lower()).strip("-")
    return slug[:SLUG_MAX_LENGTH].rstrip("-") or DEFAULT_SLUG


def stage_all(repo):
    """Stage every file in a scratch repository, ignore rules or not."""
    git(["add", "-A", "--force"], cwd=repo)


# ------------------------------------------------------------- building


def apply_overlay(skill, base, dest, with_append=True):
    """Copy base to dest, apply the skill's patches in filename order, then append.md."""
    replace_tree(base, dest)
    for patch in patches(skill):
        # dest is a plain directory, so without a ceiling git would treat a work tree around it
        # (TMPDIR inside a checkout) as the repository and skip every path outside it.
        res = git(["apply", "-p1", "--whitespace=nowarn", str(patch)], cwd=dest, check=False,
                  extra_env={"GIT_CEILING_DIRECTORIES": str(Path(dest).parent)})
        if res.returncode != 0:
            raise OverlayError(
                f"{skill}: patch {patch.name} no longer applies. Either an earlier patch it depended on "
                f"was deleted or a hand edit clashes (delete it too, run `make skills-generate "
                f"SKILL={skill}`, then redo its edit and `make skills-refresh`), or the upstream copy was changed without "
                f"`make skills-sync SKILL={skill}` (revert the copy and lock, then sync, which "
                f"carries the patches forward).\n{decode(res.stderr)}"
            )
    append = overlay_dir(skill) / APPEND_NAME
    if with_append and append.is_file():
        target = Path(dest) / SKILL_MD
        text = read_exact(target)
        write_exact(target, text + separator_for(text) + read_exact(append))


def build(skill, dest):
    apply_overlay(skill, copy_dir(skill), dest)


def verify_copy(skill):
    lock = read_lock(skill)
    if not copy_dir(skill).is_dir():
        raise OverlayError(
            f"{skill}: {rel(copy_dir(skill))} does not exist but {LOCK_NAME} does. Commit the "
            f"upstream copy (a new directory, so `git add {rel(copy_dir(skill))}`), or remove the "
            f"lock to stop mirroring the skill."
        )
    if tree_sha256(copy_dir(skill)) != lock[LOCK_SHA256_KEY]:
        raise OverlayError(
            f"{skill}: {rel(copy_dir(skill))} no longer matches the sha256 in {LOCK_NAME}. The "
            f"upstream copy was edited by hand: revert it, and change it only with "
            f"`make skills-sync SKILL={skill}`."
        )


def require_generated_matches(skill, action):
    """Refuse when the generated skill holds edits no patch records: `action` would lose them."""
    with tempfile.TemporaryDirectory() as tmp:
        build(skill, Path(tmp) / skill)
        problems = compare_trees(Path(tmp) / skill, generated_dir(skill))
    if problems:
        raise OverlayError(
            f"{skill}: {rel(generated_dir(skill))} has changes no patch records "
            f"({', '.join(problems)}). {action} would overwrite them: run "
            f"`make skills-refresh SKILL={skill}` to record them, or `make skills-generate "
            f"SKILL={skill}` to discard them, first."
        )


# ------------------------------------------------------------- upstream


def upstream_cache():
    if not (UPSTREAM_CACHE / "HEAD").exists():
        UPSTREAM_CACHE.mkdir(parents=True, exist_ok=True)
        git(["init", "-q", "--bare"], cwd=UPSTREAM_CACHE)
    # A partial clone: trees for every commit, file contents only when an export needs them.
    git(["fetch", "-q", "--tags", "--force", PARTIAL_CLONE_FILTER, upstream_url(),
         f"+refs/heads/{UPSTREAM_BRANCH}:refs/heads/{UPSTREAM_BRANCH}"], cwd=UPSTREAM_CACHE)
    return UPSTREAM_CACHE


def resolve(cache, ref):
    res = git(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd=cache, check=False)
    if res.returncode != 0:
        git(["fetch", "-q", PARTIAL_CLONE_FILTER, upstream_url(), ref], cwd=cache, check=False)
        res = git(["rev-parse", "--verify", "-q", f"{ref}^{{commit}}"], cwd=cache, check=False)
    if res.returncode != 0:
        raise OverlayError(f"upstream has no commit or ref {ref!r}")
    return decode(res.stdout).strip()


def require_on_upstream_branch(cache, commit):
    if git(["merge-base", "--is-ancestor", commit, UPSTREAM_BRANCH], cwd=cache, check=False).returncode != 0:
        raise OverlayError(
            f"commit {commit[:12]} is not on upstream's {UPSTREAM_BRANCH} branch. A commit that exists "
            f"only in a fork is not published upstream and cannot be pinned."
        )


def git_paths(args, cwd, **kw):
    """Paths from a git listing run with -z: NUL-separated, so a space or a non-ASCII byte in a
    name survives (whitespace splitting breaks the first, core.quotePath quotes the second)."""
    return [p for p in decode(git([args[0], "-z", *args[1:]], cwd, **kw).stdout).split(NUL) if p]


def refuse_ignored_files(skill, tree):
    """Refuse an upstream copy holding a file this repository's .gitignore ignores: the lock would
    hash a file `git add` never stages, so the copy would fail its check on every other checkout."""
    names = [f"{rel(copy_dir(skill))}/{name}" for name in files_of(tree)]
    # Decided without -v, which also prints paths a `!` negation re-includes; -v only explains.
    res = git(["check-ignore", "--no-index", "--stdin"], cwd=REPO_ROOT, check=False,
              input_text="\n".join(names) + "\n")
    paths = decode(res.stdout).strip()
    if paths:
        ignored = decode(git(["check-ignore", "--no-index", "-v", "--stdin"], cwd=REPO_ROOT, check=False,
                             input_text=paths + "\n").stdout).strip()
        raise OverlayError(
            f"{skill}: upstream's copy has files this repository's .gitignore ignores, so they would "
            f"never be committed and the lock could not be verified elsewhere:\n  "
            + ignored.replace("\n", "\n  ")
            + f"\nAdd a negation for them to .gitignore (for example `!{rel(copy_dir(skill))}/**`), "
            f"then run this again."
        )


def export_skill(cache, commit, skill, dest):
    """Write upstream's skills/cloud/<skill> at commit to dest. False if upstream has no such skill."""
    path = f"{UPSTREAM_SKILLS_PATH}/{skill}"
    if git(["cat-file", "-e", f"{commit}:{path}"], cwd=cache, check=False).returncode != 0:
        return False
    data = git(["archive", "--format=tar", commit, path], cwd=cache).stdout
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(tmp, filter="tar")
        replace_tree(Path(tmp) / path, dest)
    return True


def upstream_tree_id(cache, commit, skill):
    res = git(["rev-parse", "-q", "--verify", f"{commit}:{UPSTREAM_SKILLS_PATH}/{skill}"], cwd=cache, check=False)
    return decode(res.stdout).strip() if res.returncode == 0 else None


def newer_upstream_commits(cache, commit, skill):
    return git_out(["rev-list", "--count", f"{commit}..{UPSTREAM_BRANCH}", "--",
                    f"{UPSTREAM_SKILLS_PATH}/{skill}"], cwd=cache)


def staleness_notice(skill):
    try:
        cache = upstream_cache()
    except OverlayError:
        return
    commit = read_lock(skill)[LOCK_COMMIT_KEY]
    if upstream_tree_id(cache, commit, skill) != upstream_tree_id(cache, UPSTREAM_BRANCH, skill):
        print(f"note: upstream has {newer_upstream_commits(cache, commit, skill)} newer commit(s) for "
              f"{skill}. To take them, run `make skills-sync SKILL={skill}` in its own commit or PR.")


# --------------------------------------------------------- series repos


def series_repo(skill, path):
    """A scratch repo: the upstream copy as the root commit, then one commit per patch."""
    if Path(path).exists():
        shutil.rmtree(path)
    Path(path).mkdir(parents=True)
    git(["init", "-q", "-b", SERIES_BRANCH], cwd=path)
    copy_files(copy_dir(skill), path, files_of(copy_dir(skill)))
    stage_all(path)
    git(["commit", "-q", "--allow-empty", "-m", UPSTREAM_COPY_COMMIT_MESSAGE], cwd=path)
    git(["tag", BASE_TAG], cwd=path)
    for patch in patches(skill):
        git(["apply", "-p1", "--whitespace=nowarn", str(patch)], cwd=path)
        stage_all(path)
        git(["commit", "-q", "--allow-empty", "-m", patch.name], cwd=path)
    return Path(path)


def export_series(skill, repo, since, headers):
    """Rewrite the overlay's patch files from the commits since `since`. Returns surviving names."""
    names = []
    for commit in git_out(["rev-list", "--reverse", f"{since}..HEAD"], cwd=repo).split():
        name = git_out(["log", "-1", "--format=%s", commit], cwd=repo)
        diff = git_diff_text(["show", "--format=", "--no-color", "--binary", commit], cwd=repo)
        header = headers.get(name, PATCH_HEADER_TEMPLATE.format(subject=name))
        write_exact(overlay_dir(skill) / name, header + strip_index_lines(diff))
        names.append(name)
    return names


def headers_of(skill):
    return {p.name: patch_header(read_exact(p)) for p in patches(skill)}


def lines_changed_share(skill):
    total = changed = 0
    for name in files_of(generated_dir(skill)):
        new = (generated_dir(skill) / name).read_text(errors="replace").splitlines()
        old_path = copy_dir(skill) / name
        old = old_path.read_text(errors="replace").splitlines() if old_path.exists() else []
        total += len(new)
        changed += sum(1 for line in difflib.ndiff(old, new) if line.startswith("+ "))
    return 0 if not total else round(PERCENT * changed / total)


def snapshot_overlay(skill, dest):
    if overlay_dir(skill).is_dir():
        replace_tree(overlay_dir(skill), dest)


def restore_overlay(skill, snapshot):
    if Path(snapshot).is_dir():
        replace_tree(snapshot, overlay_dir(skill))


# ------------------------------------------------------------ subcommands


def cmd_generate(skill):
    require_mirrored(skill)
    with tempfile.TemporaryDirectory() as tmp:
        build(skill, Path(tmp) / skill)
        replace_tree(Path(tmp) / skill, generated_dir(skill))
    print(f"generated {rel(generated_dir(skill))}")


def cmd_check(skills):
    names = skills or mirrored_skills()
    failures = []
    for skill in names:
        try:
            require_mirrored(skill)
            verify_copy(skill)
            with tempfile.TemporaryDirectory() as tmp:
                build(skill, Path(tmp) / skill)
                problems = compare_trees(Path(tmp) / skill, generated_dir(skill))
            if problems:
                raise OverlayError(
                    f"{skill}: the committed skill differs from upstream copy + overlay:\n  "
                    + "\n  ".join(problems)
                    + f"\nIf you edited the skill, run `make skills-refresh SKILL={skill}`. If you "
                    f"edited a patch or {APPEND_NAME}, run `make skills-generate SKILL={skill}`."
                )
        except OverlayError as e:
            failures.append(str(e))
    for msg in failures:
        print(f"FAIL {msg}\n", file=sys.stderr)
    if failures:
        raise SystemExit(EXIT_FAILURE)
    print(f"ok: {len(names)} mirrored skill(s) match their upstream copy + overlay")


def strip_separator(body, base_md):
    """Drop the separator generation puts before append.md, but only when it is there: a section
    written straight under the last line leaves no separator to drop, and stripping one anyway
    records a newline-only patch."""
    sep = separator_for(base_md)
    if sep and body.endswith(sep) and body[: -len(sep)].endswith("\n") == base_md.endswith("\n"):
        return body[: -len(sep)]
    return body


def split_append(edited_md, base_md):
    """Separate the appended section from an edited SKILL.md. Returns (body, append_text or None)."""
    if APPEND_MARKER_PREFIX not in edited_md:
        return edited_md, None
    idx = edited_md.rindex(APPEND_MARKER_PREFIX)
    return strip_separator(edited_md[:idx], base_md), edited_md[idx:]


def overlap_warnings(repo):
    warnings = set()
    diff = git_out(["diff", "--cached", "-U0", "--no-color", "HEAD"], cwd=repo)
    current = None
    for line in diff.splitlines():
        if line.startswith(DIFF_OLD_FILE_PREFIX):
            current = line[len(DIFF_OLD_FILE_PREFIX):]
        m = HUNK_HEADER_RE.match(line)
        if m and current:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            if count == 0:
                continue
            blame = git_out(["blame", "--porcelain", "-L", f"{start},{start + count - 1}", "HEAD", "--", current],
                            cwd=repo, check=False)
            for commit in {b.split()[0] for b in blame.splitlines() if BLAME_LINE_RE.match(b)}:
                subject = git_out(["log", "-1", "--format=%s", commit], cwd=repo)
                if PATCH_SUBJECT_RE.fullmatch(subject):
                    warnings.add(subject)
    return sorted(warnings)


def resolve_patch_ref(skill, patch_ref):
    matches = [p.name for p in patches(skill) if p.name.startswith(patch_ref)]
    if not matches:
        raise OverlayError(f"{skill}: no patch starts with {patch_ref!r}")
    if len(matches) > 1:
        raise OverlayError(f"{skill}: {patch_ref!r} matches {', '.join(matches)}; pass the full file name")
    return matches[0]


def cmd_refresh(skill, patch_ref=None, message=None):
    require_mirrored(skill)
    verify_copy(skill)
    target = resolve_patch_ref(skill, patch_ref) if patch_ref else None
    repo = series_repo(skill, REFRESH_SCRATCH / skill)
    with tempfile.TemporaryDirectory() as tmp:
        backup = Path(tmp) / "overlay-backup"
        snapshot_overlay(skill, backup)
        try:
            edited = refresh_into_overlay(skill, repo, Path(tmp), target, message)
            # Nothing is final until the patches rebuild the edited skill exactly (the appended
            # section is written to append.md directly, so it is left out of this comparison).
            rebuilt = Path(tmp) / "rebuilt"
            apply_overlay(skill, copy_dir(skill), rebuilt, with_append=False)
            problems = compare_trees(rebuilt, edited)
            if problems:
                raise OverlayError(f"{skill}: the refreshed overlay does not rebuild the edited skill "
                                   f"({', '.join(problems)}); nothing was changed")
        except BaseException:
            restore_overlay(skill, backup)
            raise
        finally:
            shutil.rmtree(repo, ignore_errors=True)
    cmd_generate(skill)
    staleness_notice(skill)


def refresh_into_overlay(skill, repo, tmp, target, message):
    base_md = read_exact(repo / SKILL_MD) if (repo / SKILL_MD).exists() else ""
    edited = tmp / "edited"
    copy_files(generated_dir(skill), edited, committable_files(generated_dir(skill)))
    md = edited / SKILL_MD
    append_path = overlay_dir(skill) / APPEND_NAME
    if md.exists():
        body, appended = split_append(read_exact(md), base_md)
        existing = read_exact(append_path) if append_path.is_file() else None
        if appended is None and existing is not None:
            # The section was cut but the blank line before it may have stayed; that line is the
            # append's separator, not an edit to the skill.
            body = strip_separator(body, base_md)
        write_exact(md, body)
        if appended is not None and appended != existing:
            write_exact(append_path, appended)
            print(f"updated {APPEND_NAME} from the appended section")
        elif appended is None and existing is not None:
            print(f"note: SKILL.md no longer has the {APPEND_NAME} section, so regenerating restores "
                  f"it. To remove it, delete {rel(append_path)}.")
    clear_worktree(repo)
    copy_files(edited, repo, files_of(edited))
    stage_all(repo)
    if not git_out(["status", "--porcelain"], cwd=repo):
        print(f"{skill}: no change outside {APPEND_NAME} to record")
        return edited
    for name in overlap_warnings(repo):
        if name != target:
            print(f"warning: this edit changes lines that {name} introduced; consider "
                  f"`make skills-refresh SKILL={skill} PATCH={name}`")
    headers = headers_of(skill)
    if target:
        git(["commit", "-q", "-m", f"{FIXUP_PREFIX}{target}"], cwd=repo)
        res = git(["rebase", "-q", "-i", "--autosquash", BASE_TAG], cwd=repo, check=False,
                  extra_env=NON_INTERACTIVE_EDITORS)
        if res.returncode != 0:
            raise OverlayError(f"{skill}: folding into {target} conflicts with a later patch; "
                               f"refresh without PATCH= instead")
        for p in patches(skill):
            p.unlink()
        export_series(skill, repo, BASE_TAG, headers)
        print(f"folded the edit into {target}")
        return edited
    numbers = [int(p.name[:PATCH_NUMBER_WIDTH]) for p in patches(skill)]
    name = f"{(max(numbers) + 1 if numbers else 1):0{PATCH_NUMBER_WIDTH}d}-{slugify(message or DEFAULT_SLUG)}.patch"
    git(["commit", "-q", "-m", name], cwd=repo)
    header = PATCH_HEADER_TEMPLATE.format(subject=message or DEFAULT_SUBJECT)
    diff = git_diff_text(["show", "--format=", "--no-color", "--binary", "HEAD"], cwd=repo)
    write_exact(overlay_dir(skill) / name, header + strip_index_lines(diff))
    print(f"wrote {rel(overlay_dir(skill) / name)}; fill in its Why: header")
    return edited


def cmd_import(skill, ref):
    """Start mirroring a skill this repository already ships, without changing what ships.

    Writes upstream's copy at ref and its lock; the generated skill stays as it is, so the
    differences between the two are what `refresh` records as patches next.
    """
    if is_mirrored(skill):
        raise OverlayError(f"{skill} is already mirrored; use `make skills-sync SKILL={skill}`")
    if not generated_dir(skill).is_dir():
        raise OverlayError(f"{rel(generated_dir(skill))} does not exist; to adopt a new upstream "
                           f"skill, run `make skills-sync SKILL={skill}`")
    cache = upstream_cache()
    commit = resolve(cache, ref)
    require_on_upstream_branch(cache, commit)
    with tempfile.TemporaryDirectory() as tmp:
        exported = Path(tmp) / skill
        if not export_skill(cache, commit, skill, exported):
            raise OverlayError(f"upstream has no {UPSTREAM_SKILLS_PATH}/{skill} at {commit[:12]}")
        refuse_ignored_files(skill, exported)
        replace_tree(exported, copy_dir(skill))
    write_lock(skill, commit, tree_sha256(copy_dir(skill)))
    with tempfile.TemporaryDirectory() as tmp:
        build(skill, Path(tmp) / skill)
        problems = compare_trees(Path(tmp) / skill, generated_dir(skill))
    print(f"imported {skill} at upstream {commit[:12]}; the shipped skill is unchanged")
    if problems:
        print(f"  {len(problems)} file(s) differ from upstream; record them with "
              f"`make skills-refresh SKILL={skill}` (one change per patch)")


def cmd_sync(skill, ref=None):
    cache = upstream_cache()
    commit = resolve(cache, ref or UPSTREAM_BRANCH)
    require_on_upstream_branch(cache, commit)
    scratch = SCRATCH_ROOT / skill
    if scratch.exists():
        raise OverlayError(f"{skill}: a sync is already in progress in {rel(scratch)}; run "
                           f"`make skills-continue SKILL={skill}`, or delete that directory to abandon it.")
    with tempfile.TemporaryDirectory() as tmp:
        new_copy = Path(tmp) / skill
        if not export_skill(cache, commit, skill, new_copy):
            raise OverlayError(f"upstream has no {UPSTREAM_SKILLS_PATH}/{skill} at {commit[:12]}. If it was "
                               f"renamed or removed, move or remove the copy, overlay and lock by hand.")
        refuse_ignored_files(skill, new_copy)
        if not is_mirrored(skill):
            if generated_dir(skill).exists():
                raise OverlayError(
                    f"{skill}: {rel(generated_dir(skill))} already exists and is not mirrored (it has no "
                    f"lock). If it is a copy of upstream's, start mirroring it with `make skills-import "
                    f"SKILL={skill} REF=<commit>`; if it is this repository's own skill, rename one of them.")
            replace_tree(new_copy, copy_dir(skill))
            write_lock(skill, commit, tree_sha256(copy_dir(skill)))
            cmd_generate(skill)
            print(f"adopted {skill} at upstream {commit[:12]}")
            return
        verify_copy(skill)
        require_generated_matches(skill, "The sync")
        old = read_lock(skill)[LOCK_COMMIT_KEY]
        if old == commit:
            print(f"{skill}: already at upstream {commit[:12]}")
            return
        try:
            repo = series_repo(skill, scratch)
            git(["checkout", "-q", "-b", NEW_UPSTREAM_BRANCH, BASE_TAG], cwd=repo)
            clear_worktree(repo)
            copy_files(new_copy, repo, files_of(new_copy))
            stage_all(repo)
            git(["commit", "-q", "--allow-empty", "-m", f"{UPSTREAM_COPY_COMMIT_MESSAGE} @ {commit[:12]}"], cwd=repo)
            git(["checkout", "-q", SERIES_BRANCH], cwd=repo)
            shutil.copytree(new_copy, repo / ".git" / NEW_COPY_SNAPSHOT)
            state = {"commit": commit, "old": old, "headers": headers_of(skill),
                     "patches": [p.name for p in patches(skill)],
                     "why": {p.name: patch_why(p) for p in patches(skill)},
                     "conflicts": 0, "stopped": []}
            write_exact(repo / ".git" / STATE_FILE, json.dumps(state))
        except BaseException:
            shutil.rmtree(scratch, ignore_errors=True)
            raise
    res = git(["rebase", "--empty=drop", NEW_UPSTREAM_BRANCH], cwd=repo, check=False)
    continue_or_stop(skill, repo, res)


def continue_or_stop(skill, repo, res):
    state_path = repo / ".git" / STATE_FILE
    if res.returncode != 0:
        stopped = git_out(["log", "-1", "--format=%s", "REBASE_HEAD"], cwd=repo, check=False)
        if not stopped:
            shutil.rmtree(repo, ignore_errors=True)
            raise OverlayError(f"{skill}: the rebase failed without stopping on a patch; nothing was "
                               f"changed:\n{decode(res.stderr)}")
        state = json.loads(read_exact(state_path))
        state["conflicts"] += 1
        if stopped not in state["stopped"]:
            state["stopped"].append(stopped)
        # The files git left conflicted, recorded now: `git add` takes a file off that list, and
        # the other files the patch touches may hold marker-like lines on purpose.
        state["conflicted"] = git_paths(["diff", "--name-only", "--diff-filter=U"], cwd=repo, check=False)
        write_exact(state_path, json.dumps(state))
        conflicted = ", ".join(state["conflicted"])
        print(f"CONFLICT: {stopped} overlaps upstream's change in: {conflicted or '(see git status)'}\n"
              f"Fix the conflict markers in {rel(repo)}/, then run "
              f"`make skills-continue SKILL={skill}`.", file=sys.stderr)
        raise SystemExit(EXIT_CONFLICT)
    finish_sync(skill, repo)


def leftover_conflict_markers(repo, conflicted):
    """The files among those the stop left conflicted that still hold a marker line. Read from
    the working tree, so a file already staged with `git add` is still checked."""
    found = []
    for name in sorted(set(conflicted)):
        path = Path(repo) / name
        if path.is_file() and any(line.startswith(CONFLICT_MARKER_PREFIXES)
                                  for line in read_exact(path).splitlines()):
            found.append(name)
    return found


def cmd_continue(skill):
    repo = SCRATCH_ROOT / skill
    if not repo.exists():
        raise OverlayError(f"{skill}: no sync in progress")
    if not (repo / ".git" / STATE_FILE).is_file():
        raise OverlayError(f"{skill}: {rel(repo)} holds no paused sync; delete it and run the sync again")
    state = json.loads(read_exact(repo / ".git" / STATE_FILE))
    if [p.name for p in patches(skill)] != state["patches"] or headers_of(skill) != state["headers"]:
        raise OverlayError(f"{skill}: {rel(overlay_dir(skill))} changed while the sync was paused. Undo "
                           f"that change and run `make skills-continue SKILL={skill}` again, or abandon "
                           f"the sync (delete {rel(repo)}); then apply the change after the sync.")
    leftover = leftover_conflict_markers(repo, state.get("conflicted", []))
    if leftover:
        raise OverlayError(f"{skill}: conflict markers remain in {', '.join(leftover)} under {rel(repo)}/; "
                           f"finish resolving them first")
    # Tracked files only: a stray file left beside the conflict (an editor backup, a merge
    # tool's .orig) is not part of the resolution.
    git(["add", "-u"], cwd=repo)
    res = git(["rebase", "--continue"], cwd=repo, check=False, extra_env=NON_INTERACTIVE_EDITORS)
    continue_or_stop(skill, repo, res)


def finish_sync(skill, repo):
    state = json.loads(read_exact(repo / ".git" / STATE_FILE))
    for p in patches(skill):
        p.unlink()
    surviving = export_series(skill, repo, NEW_UPSTREAM_BRANCH, state["headers"])
    missing = [name for name in state["patches"] if name not in surviving]
    dropped = [name for name in missing if name in state["stopped"]]
    retired = [name for name in missing if name not in state["stopped"]]
    replace_tree(repo / ".git" / NEW_COPY_SNAPSHOT, copy_dir(skill))
    write_lock(skill, state["commit"], tree_sha256(copy_dir(skill)))
    shutil.rmtree(repo)
    cmd_generate(skill)
    print(f"\nsynced {skill}: upstream {state['old'][:12]} -> {state['commit'][:12]}")
    for name in retired:
        print(f"  retired {name} (upstream now carries it). Why: {state['why'].get(name) or '-'}")
    for name in dropped:
        print(f"  dropped {name} while resolving its conflict. Why: {state['why'].get(name) or '-'}")
    append = overlay_dir(skill) / APPEND_NAME
    if append.is_file():
        lines = read_exact(append).splitlines()
        body = "\n".join(line for line in lines if not line.startswith(APPEND_MARKER_PREFIX)).strip()
        upstream_md = copy_dir(skill) / SKILL_MD
        if body and upstream_md.exists() and body in read_exact(upstream_md):
            print(f"  {APPEND_NAME}: upstream now contains this text; delete {APPEND_NAME} and regenerate.")
    print(f"  patches: {len(patches(skill))}, share of lines changed: {lines_changed_share(skill)}%, "
          f"conflicts resolved in this sync: {state['conflicts']}")


def cmd_status():
    cache = upstream_cache()
    for skill in mirrored_skills():
        commit = read_lock(skill)[LOCK_COMMIT_KEY]
        if upstream_tree_id(cache, commit, skill) == upstream_tree_id(cache, UPSTREAM_BRANCH, skill):
            print(f"up to date  {skill}")
        else:
            print(f"behind      {skill}: {newer_upstream_commits(cache, commit, skill)} newer upstream commit(s)")
    for skill in git_paths(["ls-tree", "--name-only", f"{UPSTREAM_BRANCH}:{UPSTREAM_SKILLS_PATH}"], cwd=cache):
        if skill.startswith(UPSTREAM_SKILL_PREFIX) and not is_mirrored(skill):
            clash = " (ships here without a lock; see `make skills-import`)" if generated_dir(skill).exists() else ""
            print(f"not mirrored {skill}{clash}")


def changed_paths(base):
    """Paths changed between base and HEAD. In CI, HEAD is the pull request's merge commit, so a
    two-dot diff against the base commit is exactly the pull request's change, and it needs no
    history beyond the two commits."""
    return git_paths(["diff", "--name-only", base, "HEAD"], cwd=REPO_ROOT)


def cmd_verify_upstream(changed_since=None):
    if changed_since:
        copy_prefix = rel(COPY_ROOT) + "/"
        relevant = [f for f in changed_paths(changed_since)
                    if f.startswith(copy_prefix) or f.endswith(f"/{LOCK_NAME}")]
        if not relevant:
            print("skip: this change touches no upstream copy or lock")
            return
    cache = upstream_cache()
    failures = []
    for skill in mirrored_skills():
        try:
            commit = read_lock(skill)[LOCK_COMMIT_KEY]
            resolve(cache, commit)
            require_on_upstream_branch(cache, commit)
            with tempfile.TemporaryDirectory() as tmp:
                if not export_skill(cache, commit, skill, Path(tmp) / skill):
                    raise OverlayError(f"upstream has no {skill} at {commit[:12]}")
                problems = compare_trees(Path(tmp) / skill, copy_dir(skill))
            if problems:
                raise OverlayError(f"the copy differs from upstream at {commit[:12]}:\n  " + "\n  ".join(problems))
        except OverlayError as e:
            failures.append(f"{skill}: {e}")
    for msg in failures:
        print(f"FAIL {msg}\n", file=sys.stderr)
    if failures:
        raise SystemExit(EXIT_FAILURE)
    print(f"ok: {len(mirrored_skills())} upstream copies match google/skills at their locked commits")


def require_mirrored(skill):
    if not is_mirrored(skill):
        raise OverlayError(f"{skill} is not a mirrored skill (no {LOCK_NAME} in its overlay)")


def require_skill_name(name):
    if not SKILL_NAME_RE.fullmatch(name):
        raise OverlayError(f"{name!r} is not a skill name (lowercase letters, digits and hyphens)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync")
    p.add_argument("skill")
    p.add_argument("--ref")
    p = sub.add_parser("continue")
    p.add_argument("skill")
    p = sub.add_parser("import")
    p.add_argument("skill")
    p.add_argument("--ref", required=True)
    p = sub.add_parser("refresh")
    p.add_argument("skill")
    p.add_argument("--patch")
    p.add_argument("--message")
    p = sub.add_parser("generate")
    p.add_argument("skill")
    p = sub.add_parser("check")
    p.add_argument("skills", nargs="*")
    sub.add_parser("status")
    p = sub.add_parser("verify-upstream")
    p.add_argument("--changed-since")
    args = parser.parse_args(argv)
    try:
        for name in ([args.skill] if hasattr(args, "skill") else []) + list(getattr(args, "skills", [])):
            require_skill_name(name)
        if args.cmd == "sync":
            cmd_sync(args.skill, args.ref)
        elif args.cmd == "continue":
            cmd_continue(args.skill)
        elif args.cmd == "import":
            cmd_import(args.skill, args.ref)
        elif args.cmd == "refresh":
            cmd_refresh(args.skill, args.patch, args.message)
        elif args.cmd == "generate":
            cmd_generate(args.skill)
        elif args.cmd == "check":
            cmd_check(args.skills)
        elif args.cmd == "status":
            cmd_status()
        elif args.cmd == "verify-upstream":
            cmd_verify_upstream(args.changed_since)
    except OverlayError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_FAILURE
    return 0


if __name__ == "__main__":
    sys.exit(main())
