"""The docs map's tables stay compact: no row is column-aligned.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

`docs/README.md` carries two tables under `<!-- prettier-ignore -->`, the
generated-regions index and the identifier-sources table, and each grows a row
when a pull request adds a region or a documented identifier category. They
are compact (`| cell | cell |`, one space each side) so that a new row touches
one line: a column-aligned table re-pads every row when its widest cell
changes, and that re-padding conflicts with every open pull request adding a
row. Prettier would normalise an aligned table, but it is told to skip these,
so an editor's format-on-save that aligns one is caught by nothing else. This
test is the guard the map's own checker used to carry, kept when that checker
went with the per-document inventory.

The signature is a run of spaces immediately before a `|`, which is where
alignment padding always sits. An honest double space inside a cell's prose
is not followed by the delimiter and passes. Fenced blocks are skipped: what
is inside one is a specimen, not a table.
"""

import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAP = _REPO_ROOT / "docs" / "README.md"

# A table row is a line that opens with the delimiter once indentation is
# stripped (a table nested under a list item is still one). Two or more spaces
# and then the delimiter is padding; one space is the compact form.
_ROW_PREFIX = "|"
_ALIGNMENT_PADDING = re.compile(r" {2,}\|")
_FENCE = re.compile(r"^\s*(```|~~~)")

_COMPACT = "| Identifier | Source of truth |\n| --- | --- |\n| Go toolchain version | `go.mod` |\n"
_ALIGNED = "| Identifier          | Source of truth |\n| --- | --- |\n| Go toolchain version | `go.mod`        |\n"
_PROSE_DOUBLE_SPACE = "| a  b | c |\n"


def aligned_rows(text):
    """Return (line number, row) for every table row outside a fence that carries alignment padding."""
    rows = []
    fence = None
    for number, line in enumerate(text.splitlines(), start=1):
        opened = _FENCE.match(line)
        if opened:
            token = opened.group(1)
            if fence is None:
                fence = token
            elif token == fence:
                fence = None
            continue
        stripped = line.strip()
        if fence is None and stripped.startswith(_ROW_PREFIX) and _ALIGNMENT_PADDING.search(stripped):
            rows.append((number, stripped))
    return rows


class AlignedRowsTest(unittest.TestCase):
    def test_a_compact_table_is_clean(self):
        self.assertEqual(aligned_rows(_COMPACT), [])

    def test_an_aligned_row_is_flagged_with_its_line(self):
        self.assertEqual([number for number, _ in aligned_rows(_ALIGNED)], [1, 3])

    def test_a_double_space_inside_prose_is_not_alignment(self):
        self.assertEqual(aligned_rows(_PROSE_DOUBLE_SPACE), [])

    def test_a_row_inside_a_fence_is_a_specimen(self):
        self.assertEqual(aligned_rows("```text\n" + _ALIGNED + "```\n"), [])


class CommittedMapTest(unittest.TestCase):
    def test_the_committed_map_is_compact(self):
        flagged = aligned_rows(_MAP.read_text(encoding="utf-8"))
        self.assertEqual(
            [],
            flagged,
            "column-aligned rows in docs/README.md; rewrite them as compact"
            " `| cell | cell |` rows so a new row touches one line:\n  "
            + "\n  ".join(f"line {number}: {row[:72]}" for number, row in flagged),
        )


if __name__ == "__main__":
    unittest.main()
