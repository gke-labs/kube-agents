#!/usr/bin/env python3
"""Verify that relative links in Markdown resolve, and that every document is linked.

This catches the failure mode that actually occurs in this repository: a
relative path that was correct when written and silently broke when a file
moved or a directory was renamed. It also catches the opposite: a document
that nothing points at, which no reader will find and no review will re-read.

Scope is deliberately narrow and offline:

* relative links and image paths are resolved against the linking file and
  must point at a git-tracked file (or a directory) -- existence on disk is
  not enough, because generated or ignored files exist in a local clone but
  not in a fresh checkout or on GitHub;
* ``http(s)``, ``mailto:`` and protocol-relative links are not fetched;
* site-absolute routes (``/kube-agents/...``) are Starlight routes rather than
  paths on disk, so they are skipped -- broken ones surface as a failed site
  build in ``docs-build.yml``;
* anchors are stripped before resolution, and a bare ``#anchor`` is skipped;
* a ``docs/designs/...`` or ``docs/architecture/...`` path written inside a
  code or configuration file (the ``CODE_GLOBS`` below: Python, Go, shell,
  Dockerfiles, YAML, Terraform, TypeScript) is resolved from the repository
  root and must be git-tracked too. Comments cite design documents as the
  reasoning behind what they sit above, and a citation of a document that
  was never merged reads the same as a real one (#992). No other path in
  those files is inspected. A test fixture that needs a fake document path
  cites something like ``docs/x.md``, outside the two directories, as the
  existing ones do, or assembles the path from parts at runtime the way
  this script's own tests do; a literal in a tracked file is a citation
  like any other;
* every tracked ``.md``/``.mdx`` outside a root-level dot-directory must be
  reached by one of those: a relative link from another document, a
  repository blob URL (the form the generated skill catalogue uses; the URL
  itself is not fetched or validated), or a design-document citation from
  code. Some documents are reached by shape rather than by a link, and those
  are exempt: files at the repository root (the front door), any
  ``README.md`` (its directory reaches it), the published site under
  ``SITE_CONTENT_DIR`` (Starlight's sidebar reaches every page), and the
  uniform families in ``LINK_EXEMPT_FAMILY_GLOBS``, which a reader reaches by
  browsing the directory and which no page links one by one. The documents
  that were unlinked when the rule arrived are named in
  ``UNLINKED_ALLOWLIST``; the list only shrinks -- an entry that gains a link
  or is deleted fails the check until it is dropped.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_docs_links.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

REPO = Path(__file__).resolve().parent.parent

MARKDOWN_GLOBS = ("*.md", "*.mdx")
# Where a design document gets cited as the reasoning behind something: code,
# shell, container builds, Helm and cron configuration, Terraform, the A2A web
# client. Selected by name pattern because `git ls-files` takes one; the
# citation pattern below is conservative enough that any text file could be
# scanned, so widen this rather than exempt when a new kind of file starts
# citing designs.
CODE_GLOBS = ("*.py", "*.go", "*.sh", "*Dockerfile*", "*.yaml", "*.yml", "*.tf", "*.ts")
# The docs site's dependency tree carries its own Markdown and scripts.
VENDORED_DIR = "node_modules"

# [text](target) but not ![image](target) handled separately; both are checked.
LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")

SKIP_PREFIXES = (
    "http://",
    "https://",
    "mailto:",
    "tel:",
    "//",
    "#",
    "/kube-agents/",  # Starlight route, not a filesystem path
)

# A link to a file in this repository on GitHub, as the generated skill
# catalogue writes them. Read only for the linked-from-somewhere rule: the
# path after the prefix is the file the link reaches. Whether that file exists
# is not checked here -- an absolute URL is a remote resource to this script,
# and the catalogue is regenerated from the tree on every `make docs-generate`.
REPO_BLOB_URL_PREFIXES = (
    "https://github.com/gke-labs/kube-agents/blob/main/",
    "https://github.com/gke-labs/kube-agents/tree/main/",
)

# Fenced code blocks: links inside them are illustrative, not navigable.
FENCE_RE = re.compile(r"^\s*(```|~~~)")

# Inline code spans, for the same reason a fenced block is skipped: what is
# inside one is a specimen, not a link. Backtick runs of any length, matched
# shortest-first and longest-delimiter-first so ``a `b` c`` closes correctly.
#
# This is not hypothetical tidiness. Seven documents quote the fleet-audit
# finding-id pattern `^[a-z0-9]([a-z0-9._-]{0,98}[a-z0-9])?$`, and the `](`
# inside it reads to LINK_RE as a markdown link to `[a-z0-9._-]{0,98}[a-z0-9]`,
# which is not a file. The checker reported seven broken links in seven
# correct documents.
#
# Deliberately line-scoped: a span left unclosed on its line stays visible to
# LINK_RE, which is the safe direction to be wrong in, and line numbers in the
# report keep meaning what they say.
INLINE_CODE_RE = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)")

# A design or architecture document named from code. The match ends at `.md`,
# so a trailing `)`, `.`, `,`, `:12`, `#anchor`, a closing backtick or a
# following ` §4` is never part of the path, and the lookahead keeps `.mdx`
# from matching as `.md`. A glob (`*.md`) or an f-string (`{name}.md`) contains
# a character outside the class and is skipped, which is the safe direction.
CITATION_RE = re.compile(r"docs/(?:designs|architecture)/[A-Za-z0-9_./-]+?\.md(?![A-Za-z0-9_])")

# --- The linked-from-somewhere rule ----------------------------------------- #

# A README is reached by the directory it sits in: GitHub renders it when the
# directory is browsed, and every other tool that shows a tree does the same.
README_NAME = "README.md"

# Every page under the site's content root is in Starlight's sidebar, which is
# generated from the tree, so a page there is reached without anyone linking it.
SITE_CONTENT_DIR = "docs/site/src/content/docs/"

# Uniform families a reader reaches by browsing the directory and that no page
# links member by member: the agents' runtime material (personas, SOPs, skills
# and their references, the onboarding templates), the forge fixture READMEs,
# and the GitOps template's per-directory documents. `*` stays inside one path
# segment; `**` crosses segments. A new document in one of these directories
# needs no link; a new family needs a line here, argued in the pull request.
LINK_EXEMPT_FAMILY_GLOBS = (
    "agents/chat/defaults/onboarding/*.md",
    "agents/cluster/*.md",
    "agents/cluster/skills/*/SKILL.md",
    "agents/platform/governance/*.md",
    "agents/platform/scripts/testdata/providers/*/README.md",
    "agents/platform/skills/*/SKILL.md",
    "agents/platform/skills/*/references/*.md",
    "examples/gitops-repo/*/**",
)

# Documents nothing linked when the rule arrived. Each stays here until it is
# linked from the page that owns its topic or deleted; the check fails on an
# entry that is either, so the list can only shrink. Do not add to it: a new
# document is linked from where its readers start.
UNLINKED_ALLOWLIST = frozenset(
    {
        "a2a/persona/platform/skills/a2a-topics/SKILL.md",
        "agents/chat/AGENTS.md",
        "agents/platform/docs/autoops-architecture.md",
        "docs/designs/design_537148738.md",
        "docs/designs/e2e-testing-harness.md",
        "docs/designs/eval-next-transport.md",
        "docs/designs/fleet-anomaly-detection-checks.md",
        "docs/designs/semver-deployment-versioning.md",
        "docs/designs/upgrade-readiness-checks.md",
    }
)

# This script names the allowlist's paths as literals, which the citation scan
# above reads as citations. They are the list, not a reader's path to the
# document, so this file is not a source for the linked-from-somewhere rule.
# Its citations are still checked for existence like any other file's, so a
# deleted design document is reported here as a broken citation as well.
SELF = Path(__file__).resolve()

UNLINKED_MESSAGE = "linked from nowhere -- link it from the page that owns its topic"
ALLOWLIST_LINKED_MESSAGE = "in UNLINKED_ALLOWLIST but is now linked -- drop the entry"
ALLOWLIST_UNTRACKED_MESSAGE = "in UNLINKED_ALLOWLIST but is not tracked -- drop the entry"


def tracked_paths() -> set[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return {(REPO / p).resolve() for p in out if p and (REPO / p).is_file()}


def tracked_files(patterns: tuple[str, ...]) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", *patterns],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [REPO / p for p in out if p and VENDORED_DIR not in p and (REPO / p).is_file()]


def tracked_markdown() -> list[Path]:
    return tracked_files(MARKDOWN_GLOBS)


def tracked_code() -> list[Path]:
    return tracked_files(CODE_GLOBS)


def strip_code_fences(text: str) -> list[tuple[int, str]]:
    """Return (line_number, line) for lines outside fenced code blocks."""
    kept: list[tuple[int, str]] = []
    fence: str | None = None
    for n, line in enumerate(text.splitlines(), start=1):
        m = FENCE_RE.match(line)
        if m:
            token = m.group(1)
            if fence is None:
                fence = token
            elif token == fence:
                fence = None
            continue
        if fence is None:
            kept.append((n, line))
    return kept


def markdown_links(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line number, link target) for every navigable link in a document."""
    for lineno, line in strip_code_fences(path.read_text(encoding="utf-8")):
        # A space, not "", so stripping a span cannot glue a stray `[text]`
        # onto a following `(target)` and invent a link that was never written.
        line = INLINE_CODE_RE.sub(" ", line)
        for raw in LINK_RE.findall(line):
            target = raw.strip()
            if target:
                yield lineno, target


def link_target(path: Path, target: str) -> Path | None:
    """The file a link denotes, unresolved, or None when it names no file here.

    A repository blob URL denotes the path after its prefix; any other
    absolute URL, a mail or phone link, a bare anchor and a Starlight route
    denote nothing on disk. A leading ``/`` is repository-root-relative;
    anything else is relative to the linking document.
    """
    for prefix in REPO_BLOB_URL_PREFIXES:
        if target.startswith(prefix):
            return REPO / unquote(target[len(prefix) :].split("#", 1)[0])
    if target.startswith(SKIP_PREFIXES):
        return None
    # drop any anchor, then percent-decode
    file_part = unquote(target.split("#", 1)[0])
    if not file_part:
        return None
    if file_part.startswith("/"):
        return REPO / file_part.lstrip("/")
    return path.parent / file_part


def code_citations(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line number, cited path) for every design-document path in a code file.

    Citations are repository-root paths by convention, so nothing is resolved
    relative to the citing file. Code fences and inline code are not stripped
    here: in a comment, backticks are how a path is quoted, not a sign that it
    is a specimen.
    """
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for m in CITATION_RE.finditer(line):
            yield lineno, m.group(0)


def check_file(path: Path, tracked: set[Path]) -> list[str]:
    problems: list[str] = []
    for lineno, target in markdown_links(path):
        if target.startswith(REPO_BLOB_URL_PREFIXES):
            continue  # a remote resource to this script; see REPO_BLOB_URL_PREFIXES
        resolved = link_target(path, target)
        if resolved is None:
            continue
        if resolved.resolve() not in tracked and not resolved.is_dir():
            rel = path.relative_to(REPO)
            problems.append(f"{rel}:{lineno}: broken link -> {target}")
    return problems


def check_code_file(path: Path, tracked: set[Path]) -> list[str]:
    """Report every design-document path cited in a code file that is not tracked."""
    problems: list[str] = []
    for lineno, cited in code_citations(path):
        if (REPO / cited).resolve() not in tracked:
            rel = path.relative_to(REPO)
            problems.append(f"{rel}:{lineno}: broken citation -> {cited}")
    return problems


def linked_files(markdown: list[Path], code: list[Path]) -> set[Path]:
    """Every file some document links or some code file cites, resolved."""
    reached: set[Path] = set()
    for path in markdown:
        for _, target in markdown_links(path):
            resolved = link_target(path, target)
            if resolved is not None:
                reached.add(resolved.resolve())
    for path in code:
        if path.resolve() == SELF:
            continue
        for _, cited in code_citations(path):
            reached.add((REPO / cited).resolve())
    return reached


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a family glob to a regex: `**` crosses slashes, `*` does not."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


FAMILY_PATTERNS = tuple(glob_to_regex(glob) for glob in LINK_EXEMPT_FAMILY_GLOBS)


def reached_by_shape(rel: str) -> bool:
    """True for a document a reader reaches without anyone linking it.

    ``rel`` is repository-relative with forward slashes. Root-level
    dot-directories are tooling, not documentation, and are out of scope by
    the same rule as before; a dot-directory nested inside a documented area
    (``examples/gitops-repo/.github/``) is content and stays in scope.
    """
    if "/" not in rel or rel.startswith("."):
        return True
    if rel.rsplit("/", 1)[1] == README_NAME:
        return True
    if rel.startswith(SITE_CONTENT_DIR):
        return True
    return any(pattern.match(rel) for pattern in FAMILY_PATTERNS)


def check_unlinked(markdown: list[Path], reached: set[Path]) -> list[str]:
    """Report every document nothing links, and every allowlist entry that is stale."""
    problems: list[str] = []
    tracked_rel = {path.relative_to(REPO).as_posix() for path in markdown}
    for rel in sorted(UNLINKED_ALLOWLIST - tracked_rel):
        problems.append(f"{rel}: {ALLOWLIST_UNTRACKED_MESSAGE}")
    for path in markdown:
        rel = path.relative_to(REPO).as_posix()
        linked = path.resolve() in reached
        if rel in UNLINKED_ALLOWLIST:
            if linked:
                problems.append(f"{rel}: {ALLOWLIST_LINKED_MESSAGE}")
            continue
        if linked or reached_by_shape(rel):
            continue
        problems.append(f"{rel}: {UNLINKED_MESSAGE}")
    return problems


def main() -> int:
    files = tracked_markdown()
    if not files:
        print("ERROR: no Markdown files found.", file=sys.stderr)
        return 1

    tracked = tracked_paths()
    problems: list[str] = []
    for f in files:
        problems.extend(check_file(f, tracked))

    code = tracked_code()
    for f in code:
        problems.extend(check_code_file(f, tracked))

    problems.extend(check_unlinked(files, linked_files(files, code)))

    print(
        f"Checked relative links in {len(files)} Markdown files "
        f"and design-doc citations in {len(code)} code files, "
        f"and that every document is linked from somewhere "
        f"({len(UNLINKED_ALLOWLIST)} allowlisted)."
    )
    if problems:
        print(
            f"\n{len(problems)} broken link(s), citation(s) or unlinked document(s):\n",
            file=sys.stderr,
        )
        for p in problems:
            print(f"    {p}", file=sys.stderr)
        return 1
    print("All relative links and design-doc citations resolve, and every document is linked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
