#!/usr/bin/env python3
"""Verify that relative links in Markdown resolve to files that exist.

This catches the failure mode that actually occurs in this repository: a
relative path that was correct when written and silently broke when a file
moved or a directory was renamed.

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
  like any other.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_docs_links.py
"""

from __future__ import annotations

import re
import subprocess
import sys
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


def check_file(path: Path, tracked: set[Path]) -> list[str]:
    problems: list[str] = []
    for lineno, line in strip_code_fences(path.read_text(encoding="utf-8")):
        # A space, not "", so stripping a span cannot glue a stray `[text]`
        # onto a following `(target)` and invent a link that was never written.
        line = INLINE_CODE_RE.sub(" ", line)
        for raw in LINK_RE.findall(line):
            target = raw.strip()
            if not target or target.startswith(SKIP_PREFIXES):
                continue
            # drop any anchor, then percent-decode
            file_part = unquote(target.split("#", 1)[0])
            if not file_part:
                continue
            resolved = (
                (REPO / file_part.lstrip("/"))
                if file_part.startswith("/")
                else (path.parent / file_part)
            )
            if resolved.resolve() not in tracked and not resolved.is_dir():
                rel = path.relative_to(REPO)
                problems.append(f"{rel}:{lineno}: broken link -> {target}")
    return problems


def check_code_file(path: Path, tracked: set[Path]) -> list[str]:
    """Report every design-document path cited in a code file that is not tracked.

    Citations are repository-root paths by convention, so nothing is resolved
    relative to the citing file. Code fences and inline code are not stripped
    here: in a comment, backticks are how a path is quoted, not a sign that it
    is a specimen.
    """
    problems: list[str] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for m in CITATION_RE.finditer(line):
            cited = m.group(0)
            if (REPO / cited).resolve() not in tracked:
                rel = path.relative_to(REPO)
                problems.append(f"{rel}:{lineno}: broken citation -> {cited}")
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

    print(
        f"Checked relative links in {len(files)} Markdown files "
        f"and design-doc citations in {len(code)} code files."
    )
    if problems:
        print(f"\n{len(problems)} broken link(s) or citation(s):\n", file=sys.stderr)
        for p in problems:
            print(f"    {p}", file=sys.stderr)
        return 1
    print("All relative links and design-doc citations resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
