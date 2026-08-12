#!/usr/bin/env python3
"""Verify that the documentation map inventories every Markdown document.

``docs/README.md`` is the hand-maintained map of every ``.md``/``.mdx`` file in
the repository. Hand-maintained means it drifts: a PR adds, moves, renames, or
deletes a document and forgets the map. This check makes that drift a CI
failure instead of a review-time hope. Three checks:

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
* **Padding** -- every table row in the map must use compact single-space
  padding (``| cell | cell |``). Prettier aligns Markdown table columns to the
  widest cell, so a row wider than the current column re-pads every other row
  in the table; the map's tables carry ``<!-- prettier-ignore -->`` to stop
  that, and this check stops a hand edit (or an editor-on-save with a stale
  config) from re-introducing it. The map is edited from several branches every
  week: re-aligning a table rewrites bytes those branches also touch, and turns
  a one-line insertion into a conflict on every open pull request. What is
  matched is a run of spaces immediately before a ``|`` -- where prettier's
  padding always lands -- so a double space inside a prose cell is not a
  failure.

Deliberately NOT checked: any *count*. The map used to state a repository
document total and a per-family file count, and both were verified here. They
were removed because a count is a single line every concurrent PR must edit,
which made the map this repository's most frequent merge conflict. The totals
are derived below and printed, never stored.

Also NOT checked: the prose summaries and mentions outside the inventory
tables. Those stay on PR review (the ``review-docs-drift`` skill); this script
guarantees presence and shape, nothing more.

Standard library only, so it runs in CI and in a bare clone.

Usage::

    python3 scripts/check_docs_map.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
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

# The signature of a prettier-style column alignment. Prettier pads a cell out
# to the width of the widest one in its column, and that padding run always
# sits immediately before the next `|`. Matching the run *plus* the delimiter,
# rather than two spaces anywhere, lets an honest double space inside prose
# through -- the tables are prettier-ignored, so nothing would normalise it
# away and the author would have no way to satisfy the check.
ALIGNMENT_PADDING = "  |"


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


def realigned_rows(text: str) -> list[tuple[int, str]]:
    """Return (line number, row) for every table row that was column-aligned.

    Scans the whole map, not just the inventory: the identifier-sources table
    in section 2 churns as hard as the inventory does. Rows are matched on the
    stripped line, the same as ``inventory_rows`` -- an indented table (one
    nested under a list item) is still a table prettier will re-align.
    """
    rows = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("|") and ALIGNMENT_PADDING in stripped:
            rows.append((number, stripped))
    return rows


def inventory_tokens(text: str) -> Iterator[tuple[int, str]]:
    """Yield (cell index, path token) for every path-like token in the inventory.

    One reader, two consumers: the coverage and existence checks in ``main``,
    and the collapsed-family roster in ``generate_docs.py``. Sharing it is what
    makes the roster's guarantee structural — a second extraction with its own
    idea of where a glob may sit would let a row satisfy coverage from a cell
    the roster never reads, and a deletion inside that family would then be
    invisible to both checks.
    """
    for row in inventory_rows(text):
        cells = [c.strip() for c in row.strip("|").split("|")]
        for cell_index, cell in enumerate(cells):
            for token in TOKEN_RE.findall(cell):
                if looks_like_doc_path(token):
                    yield cell_index, token


def family_globs(text: str) -> set[str]:
    """Return every collapsed-family glob the coverage check honours."""
    return {token for _, token in inventory_tokens(text) if "*" in token}


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

    covered: set[str] = set()
    stale: list[tuple[str, str]] = []  # (row path-cell token, reason)

    # Coverage may come from a token in any cell of the row; existence is only
    # enforced for the path column (the first cell), where every token is a
    # deliberate path claim rather than prose.
    for cell_index, token in inventory_tokens(text):
        hits = matches(token, files)
        covered |= hits
        if cell_index == 0 and not hits and token != SELF:
            stale.append((token, "matches no tracked .md/.mdx file"))

    required = {f for f in files if not in_dot_dir(f)}
    missing = sorted(required - covered)
    realigned = realigned_rows(text)

    ok = True
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
    if realigned:
        ok = False
        print(f"{len(realigned)} column-aligned table row(s) in {MAP.relative_to(REPO)} "
              "-- re-pad them as `| cell | cell |`; aligning a table rewrites every "
              "row and conflicts with every open pull request:")
        for number, line in realigned:
            print(f"  PADDING  line {number}: {line[:72]}…")
    if ok:
        exempt = len(files) - len(required)
        print(
            f"Documentation map inventory covers all {len(required)} tracked docs "
            f"({total_actual} counting the map itself), no stale path cells, no "
            f"re-aligned table rows ({exempt} root-level dot-directory tooling "
            "files exempt)."
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
