#!/usr/bin/env python3
"""Relabel the upgrade table's `pending` rows that a release already contains.

    python3 scripts/relabel_upgrade_table_pending.py

Run from a full clone of main with the release tags fetched. For each row of the "Breaking changes in
recent releases" table still labelled `pending`, it asks check_upgrade_table_releases.py's own rule
(pending_row_release) which release contains the row, and rewrites the row's Release cell to that
release, so a relabel this script writes is one the check accepts. Rows no release contains stay
`pending`. It edits the page in the working tree and prints one line per relabelled row,
`<release>\t<change>`, so a caller can say what changed. It prints nothing when there is nothing to
relabel.

The release job runs it after cutting a tag (scripts/release/open_upgrade_relabel_pr.sh), so main's
`make docs-check-upgrade-releases` is not left failing until someone relabels the rows by hand.

The table is prettier-aligned. The new label is padded to the cell's existing width; a label longer
than that width is written as is, and the caller runs prettier on the page.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_upgrade_table_releases as check  # noqa: E402  (beside this file, not an installed module)

# A row's Release cell while it is `pending`: the label and the padding prettier gave the column.
PENDING_CELL = re.compile(rf"^\| (?P<cell>{check.PENDING} *)\|")
CELL_PREFIX = "| "
CELL_SUFFIX = "|"
CELL_PAD = " "
OUTPUT_SEPARATOR = "\t"
LINE_BREAK = "\n"


def relabels(page: str, tags: set[str], traced: dict[str, str]) -> tuple[list[tuple[check.Row, str]], list[str]]:
    """Each `pending` row of `page` that a release contains, with that release, in page order; and why any
    such row is left `pending`.

    A row is relabelled only when the check would accept the new label (labelled_row_problem). The one
    case it would not: a row TRACES_FILE names a main commit for, which reached a release by a cherry-pick
    that release's table holds. The check reads a labelled traced row by its commit alone, so that row is
    left for a person to label and trace.
    """
    found, left = [], []
    for row in check.rows(page):
        if row.release != check.PENDING:
            continue
        shipping = check.pending_row_release(row, tags, traced)
        if shipping is None:
            continue
        labelled = check.Row(row.line, shipping[0], row.change)
        if problem := check.labelled_row_problem(labelled, traced):
            left.append(f"left {check.PENDING}: {problem}")
            continue
        found.append((row, shipping[0]))
    return found, left


def relabelled(page: str, labels: list[tuple[check.Row, str]]) -> str:
    """`page` with each listed row's Release cell set to its release, padded to the cell's width."""
    lines = page.split(LINE_BREAK)
    for row, release in labels:
        index = row.line - 1
        match = PENDING_CELL.match(lines[index])
        if match is None:
            raise ValueError(f"{check.UPGRADE_PAGE}:{row.line}: expected a {check.PENDING} Release cell")
        # At least one space before the cell's closing pipe, as prettier writes it.
        cell = release.ljust(max(len(match["cell"]), len(release) + len(CELL_PAD)))
        lines[index] = CELL_PREFIX + cell + lines[index][match.end() - len(CELL_SUFFIX):]
    return LINE_BREAK.join(lines)


def main() -> int:
    problem = check.clone_problem()
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return check.EXIT_FAILED
    traced, _ = check.traces()
    page_path = Path(check.git("rev-parse", "--show-toplevel").strip()) / check.UPGRADE_PAGE
    page = page_path.read_text(encoding="utf-8")
    labels, left = relabels(page, check.release_tags(), traced)
    for note in left:
        print(f"{check.WARNING_PREFIX}{note}", file=sys.stderr)
    if labels:
        page_path.write_text(relabelled(page, labels), encoding="utf-8")
    for row, release in labels:
        print(f"{release}{OUTPUT_SEPARATOR}{row.change}")
    return check.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
