#!/usr/bin/env python3
"""Verify that the documentation map inventories every Markdown document.

``docs/README.md`` is the hand-maintained map of every ``.md``/``.mdx`` file in
the repository. Hand-maintained means it drifts: a PR adds, moves, renames, or
deletes a document and forgets the map. This check makes that drift a CI
failure instead of a review-time hope. Four checks:

* **Coverage** -- every git-tracked ``.md``/``.mdx`` file must be matched by at
  least one backticked path (or glob) somewhere in the map's inventory tables
  (section 4). Collapsed family rows use globs (``agents/platform/skills/*/
  SKILL.md``, ``examples/gitops-repo/**``), so one row can cover many files.
  Files under a ROOT-LEVEL dot-directory (``.agents/``, ``.github/``,
  ``.claude/``, …) are tooling, not documentation: the map does not inventory
  them and this check does not require them — the map and the check share one
  scope. A dot-directory nested inside a documented area (e.g.
  ``examples/gitops-repo/.github/``) is example content and IS required.
* **Existence** -- every backticked path in the *first column* of an inventory
  row must match at least one tracked file. A path cell that matches nothing is
  a stale row: the file was moved or deleted and the map was not updated.
* **Total** -- the document total the map states in section 1 ("tracks **N**
  ``.md``/``.mdx`` documents") must equal the number of tracked docs outside
  root-level dot-directories.
* **Family counts** -- when a collapsed family row annotates its glob with a
  count, as in ``agents/platform/skills/*/SKILL.md`` *(17 skills)*, that count
  must equal the number of tracked files the glob matches.

Deliberately NOT checked: the prose summaries and mentions outside the
inventory tables. Those stay on PR review (the ``review-docs-drift`` skill);
this script guarantees presence and counts, nothing more.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_docs_map.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAP = REPO / "docs" / "README.md"

# The inventory starts at section 4 and ends at the next ## heading.
INVENTORY_START = "## 4. Inventory"

# Backticked tokens that plausibly denote a doc path or path glob.
TOKEN_RE = re.compile(r"`([^`]+)`")

# Site-page rows are written relative to the site content root (their table
# says so in its header); try this prefix when a token does not resolve as-is.
SITE_PREFIX = "docs/site/src/content/docs/"

# The map does not inventory itself; section 1 declares it ("this map").
SELF = "docs/README.md"

# The stated total in section 1: "tracks **138** `.md`/`.mdx` documents".
# \s+ tolerates the sentence being re-wrapped across lines.
TOTAL_RE = re.compile(r"tracks\s+\*\*(\d+)\*\*\s+`\.md`/`\.mdx`\s+documents")

# A family row's count annotation next to its glob: "(17 skills)", "(9 files)".
FAMILY_COUNT_RE = re.compile(r"\((\d+)[^)]*\)")


def in_dot_dir(path: str) -> bool:
    """True for paths under a root-level dot-directory (.agents/, .github/, …).

    Those hold tooling artifacts — review skills, PR/issue templates, style
    guides, local agent config — not documentation a reader navigates. They
    are out of the map's scope by the same rule, so new tooling files never
    force a map edit. Only the FIRST path segment counts: a dot-directory
    nested inside a documented area (examples/gitops-repo/.github/) is part of
    that example and stays in scope. (A dot-dir path in a path cell would
    still be validated for existence, keeping the two scopes from silently
    diverging.)
    """
    return path.split("/", 1)[0].startswith(".")


def tracked_docs() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.md", "*.mdx"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return {p for p in out.stdout.split("\0") if p}


def inventory_rows(text: str) -> list[str]:
    """Return the markdown table rows of the inventory section."""
    try:
        body = text.split(INVENTORY_START, 1)[1]
    except IndexError:
        sys.exit(f"ERROR: {MAP} has no '{INVENTORY_START}' section")
    rows = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        # Skip header-separator rows (|---|---|).
        if set(stripped) <= {"|", "-", ":", " "}:
            continue
        rows.append(stripped)
    return rows


def looks_like_doc_path(token: str) -> bool:
    return (token.endswith((".md", ".mdx")) or token.endswith("/**")) and " " not in token


def pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a map glob to a regex. `**` crosses slashes, `*` does not."""
    out = []
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


def matches(pattern: str, files: set[str]) -> set[str]:
    hits = set()
    for candidate in (pattern, SITE_PREFIX + pattern):
        rx = pattern_to_regex(candidate)
        hits |= {f for f in files if rx.match(f)}
        if hits:
            break
    return hits


def main() -> int:
    files = tracked_docs()
    total_actual = sum(1 for f in files if not in_dot_dir(f))
    files.discard(SELF)
    text = MAP.read_text(encoding="utf-8")
    rows = inventory_rows(text)

    covered: set[str] = set()
    stale: list[tuple[str, str]] = []  # (row path-cell token, reason)
    miscounted: list[tuple[str, int, int]] = []  # (glob, stated, actual)

    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        if not cells:
            continue
        # Coverage may come from a token in any cell of the row; existence is
        # only enforced for the path column (the first cell), where every
        # token is a deliberate path claim rather than prose.
        for cell_index, cell in enumerate(cells):
            for token in TOKEN_RE.findall(cell):
                if not looks_like_doc_path(token):
                    continue
                hits = matches(token, files)
                covered |= hits
                if cell_index == 0 and not hits and token != SELF:
                    stale.append((token, "matches no tracked .md/.mdx file"))
                # A glob row may annotate itself with a file count; hold it
                # to what the glob actually matches.
                if cell_index == 0 and "*" in token:
                    count = FAMILY_COUNT_RE.search(cell)
                    if count and int(count.group(1)) != len(hits):
                        miscounted.append((token, int(count.group(1)), len(hits)))

    required = {f for f in files if not in_dot_dir(f)}
    missing = sorted(required - covered)

    ok = True
    stated = TOTAL_RE.search(text)
    if not stated:
        ok = False
        print("Section 1 states no document total the check can parse "
              "(expected: tracks **N** `.md`/`.mdx` documents).")
    elif int(stated.group(1)) != total_actual:
        ok = False
        print(f"Stated document total is {stated.group(1)}, but git tracks "
              f"{total_actual} .md/.mdx files outside root-level dot-directories.")
    if missing:
        ok = False
        print(f"{len(missing)} tracked doc(s) missing from the map inventory ({MAP.relative_to(REPO)}):")
        for f in missing:
            print(f"  MISSING  {f}")
    if stale:
        ok = False
        print(f"{len(stale)} stale path(s) in the map's inventory path column:")
        for token, reason in stale:
            print(f"  STALE    `{token}` -- {reason}")
    if miscounted:
        ok = False
        print(f"{len(miscounted)} family row(s) whose count disagrees with their glob:")
        for token, claimed, actual in miscounted:
            print(f"  COUNT    `{token}` -- row says {claimed}, glob matches {actual}")
    if ok:
        exempt = len(files) - len(required)
        print(
            f"Documentation map inventory covers all {len(required)} tracked docs, "
            f"stated total {total_actual} matches, no stale path cells or family-count "
            f"mismatches ({exempt} root-level dot-directory tooling files exempt)."
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
