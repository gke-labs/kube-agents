#!/usr/bin/env python3
"""Unit tests for the two guards the documentation map's mergeability rests on.

Run: cd scripts && python3 -m unittest test_docs_map

Both guards fail *green*, which is why they are tested rather than watched. A
padding check that stops matching lets prettier re-align the map on the next
hand edit, and the conflict-on-every-open-PR problem it exists to prevent comes
back with no signal. A family-glob extraction that stops matching a row makes
``make docs-generate`` rewrite ``docs/family-roster.txt`` to the smaller set;
``docs-check-generated`` then passes against the new snapshot, and that
family's deletion guard is gone.
"""

import unittest

import check_docs_map
import generate_docs

REPO = check_docs_map.REPO

COMPACT = "| Path | Purpose |\n| --- | --- |\n| `a.md` | Short |\n"
ALIGNED = "| Path   | Purpose |\n| ------ | ------- |\n| `a.md` | Short   |\n"


class RealignedRowsTest(unittest.TestCase):
    """`realigned_rows` -- the padding check."""

    def test_compact_table_is_clean(self):
        self.assertEqual(check_docs_map.realigned_rows(COMPACT), [])

    def test_flags_a_prettier_aligned_row(self):
        flagged = check_docs_map.realigned_rows(ALIGNED)
        self.assertEqual([number for number, _ in flagged], [1, 3])

    def test_ignores_a_double_space_inside_prose(self):
        """An honest double space is not alignment, and nothing can normalise it.

        The tables are prettier-ignored, so a check that fired on two spaces
        anywhere would leave the author no way to satisfy it.
        """
        text = "| `a.md` | Ends here.  Starts again. |\n"
        self.assertEqual(check_docs_map.realigned_rows(text), [])

    def test_flags_an_indented_aligned_row(self):
        """A table nested under a list item is still a table prettier re-aligns."""
        text = "  | `a.md` | Short   |\n"
        self.assertEqual([number for number, _ in check_docs_map.realigned_rows(text)], [1])

    def test_indented_compact_row_is_clean(self):
        self.assertEqual(check_docs_map.realigned_rows("  | `a.md` | Short |\n"), [])

    def test_the_committed_map_is_compact(self):
        text = check_docs_map.MAP.read_text(encoding="utf-8")
        self.assertEqual(check_docs_map.realigned_rows(text), [])


class FamilyGlobsTest(unittest.TestCase):
    """`family_globs` -- the roster's view of which rows are families."""

    def _map(self, *rows: str) -> str:
        return check_docs_map.INVENTORY_START + "\n\n" + "".join(f"{row}\n" for row in rows)

    def test_reads_a_glob_from_a_cell_other_than_the_path_column(self):
        """Coverage accepts a glob from any cell, so the roster must too.

        A row satisfying coverage from a cell the roster never read would leave
        a family unrostered -- a deletion inside it invisible to both checks,
        which is the exact gap the roster exists to close.
        """
        text = self._map("| `docs/thing.md` | Guide | Also covers `docs/extra/*.md`. |")
        self.assertEqual(check_docs_map.family_globs(text), {"docs/extra/*.md"})

    def test_ignores_non_path_tokens_and_plain_paths(self):
        text = self._map("| `docs/thing.md` | Guide | Run `make docs-check` first. |")
        self.assertEqual(check_docs_map.family_globs(text), set())

    def test_finds_the_real_map_families(self):
        text = check_docs_map.MAP.read_text(encoding="utf-8")
        globs = check_docs_map.family_globs(text)
        self.assertIn("agents/platform/governance/*.md", globs)
        self.assertIn("agents/platform/skills/*/SKILL.md", globs)


class FamilyRosterTest(unittest.TestCase):
    """`gen_family_roster` -- the deletion guard for collapsed families."""

    @classmethod
    def setUpClass(cls):
        cls.roster = generate_docs.gen_family_roster()
        cls.map_text = check_docs_map.MAP.read_text(encoding="utf-8")

    def test_lists_every_member_of_a_known_family(self):
        """Expectation built from a literal path prefix, not from the glob machinery.

        Sharing `matches()` with the code under test would make this tautological.
        """
        expected = {
            f
            for f in check_docs_map.tracked_docs()
            if f.startswith("agents/platform/governance/") and f.endswith(".md")
        }
        self.assertTrue(expected, "no governance SOPs tracked -- the fixture, not the roster, broke")
        missing = sorted(f for f in expected if f"\n  {f}\n" not in self.roster)
        self.assertEqual(missing, [], "family members absent from the roster")

    def test_rosters_every_family_glob_the_map_declares(self):
        for glob in sorted(check_docs_map.family_globs(self.map_text)):
            with self.subTest(glob=glob):
                self.assertIn(f"\n{glob}\n", self.roster)

    def test_committed_roster_matches_the_generator(self):
        committed = generate_docs.ROSTER_FILE.read_text(encoding="utf-8")
        self.assertEqual(committed, self.roster, "run: make docs-generate")


if __name__ == "__main__":
    unittest.main()
