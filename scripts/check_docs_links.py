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
* anchors are stripped before resolution, and a bare ``#anchor`` is skipped.

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


def tracked_paths() -> set[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return {(REPO / p).resolve() for p in out if p}


def tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.md", "*.mdx"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [REPO / p for p in out if p and "node_modules" not in p]


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


def main() -> int:
    files = tracked_markdown()
    if not files:
        print("ERROR: no Markdown files found.", file=sys.stderr)
        return 1

    tracked = tracked_paths()
    problems: list[str] = []
    for f in files:
        problems.extend(check_file(f, tracked))

    print(f"Checked relative links in {len(files)} Markdown files.")
    if problems:
        print(f"\n{len(problems)} broken link(s):\n", file=sys.stderr)
        for p in problems:
            print(f"    {p}", file=sys.stderr)
        return 1
    print("All relative links resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
