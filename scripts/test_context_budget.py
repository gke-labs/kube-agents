#!/usr/bin/env python3
"""Unit tests for the always-loaded instruction files' context budget.

Run: cd scripts && python3 -m unittest test_context_budget

Import handling is where this check goes wrong quietly, so it is tested rather
than trusted. Charge an import too little -- drop the line, or lose the
recursion into the file it names -- and moving a section out to
``@docs/page.md`` registers as a saving the harness never made. Charge it twice
-- lose the ``seen`` set that ``measure`` shares across the roots, so
CLAUDE.md's ``@AGENTS.md`` re-expands a file already counted -- and the check
starts failing for a reason no pull request caused, whose obvious fix (raise
BUDGET) hides the real size.

The budget assertion at the end is the check itself, run against the real
files, so ``python3 -m unittest`` catches an over-budget tree even where the
Makefile target is not wired in.
"""

import contextlib
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import check_context_budget


def run_main() -> tuple[int, str]:
    """`main()`'s exit code and what it printed, with stdout kept out of the log."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = check_context_budget.main()
    return code, out.getvalue()


class IsImportTest(unittest.TestCase):
    """`is_import` -- what gets excluded from the char count."""

    def test_bare_import_directive(self):
        self.assertTrue(check_context_budget.is_import("@AGENTS.md\n"))

    def test_indented_import_directive(self):
        self.assertTrue(check_context_budget.is_import("  @AGENTS.md  \n"))

    def test_prose_mentioning_an_at_sign_is_content(self):
        self.assertFalse(check_context_budget.is_import("@AGENTS.md is the entry point\n"))

    def test_email_style_handle_alone_is_not_a_path(self):
        # `@me` has no space either, so only the path shape tells the two apart.
        # It is charged as content, which is the safe direction -- over-counting
        # fails loudly, under-counting hides growth.
        self.assertFalse(check_context_budget.is_import("@me\n"))

    def test_handle_with_a_hyphen_is_not_a_path(self):
        self.assertFalse(check_context_budget.is_import("@platform-agent\n"))

    def test_tab_separated_trailer_is_not_an_import(self):
        self.assertFalse(check_context_budget.is_import("@AGENTS.md\tsee also\n"))

    def test_bare_at_is_not_an_import(self):
        self.assertFalse(check_context_budget.is_import("@\n"))

    def test_ordinary_line(self):
        self.assertFalse(check_context_budget.is_import("- Keep changes scoped.\n"))


class LoadedSizeTest(unittest.TestCase):
    """`loaded_size` -- an import costs what the harness loads for it."""

    def test_content_is_counted_whole(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "AGENTS.md"
            path.write_text("# Title\n\nbody\n", encoding="utf-8")
            self.assertEqual(check_context_budget.loaded_size(path), len("# Title\n\nbody\n"))

    def test_import_is_charged_the_target_not_the_line(self):
        # The failure this stops: moving a section out to `@docs/page.md` reads
        # as a saving while the harness still loads every character of it.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("a" * 500 + "\n", encoding="utf-8")
            path = root / "CLAUDE.md"
            path.write_text("@AGENTS.md\nrule\n", encoding="utf-8")
            self.assertEqual(check_context_budget.loaded_size(path), 501 + len("rule\n"))

    def test_import_resolves_relative_to_the_importing_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "page.md").write_text("body\n", encoding="utf-8")
            path = root / "CLAUDE.md"
            path.write_text("@docs/page.md\n", encoding="utf-8")
            self.assertEqual(check_context_budget.loaded_size(path), len("body\n"))

    def test_missing_target_is_charged_as_text(self):
        # The harness has nothing to expand either, so the line stays on screen.
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "CLAUDE.md"
            path.write_text("@docs/gone.md\n", encoding="utf-8")
            self.assertEqual(check_context_budget.loaded_size(path), len("@docs/gone.md\n"))

    def test_a_file_reached_twice_is_charged_once(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "shared.md").write_text("x" * 100 + "\n", encoding="utf-8")
            path = root / "CLAUDE.md"
            path.write_text("@shared.md\n@shared.md\n", encoding="utf-8")
            self.assertEqual(check_context_budget.loaded_size(path), 101)

    def test_an_import_cycle_terminates(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.md").write_text("@b.md\nA\n", encoding="utf-8")
            (root / "b.md").write_text("@a.md\nB\n", encoding="utf-8")
            self.assertEqual(check_context_budget.loaded_size(root / "a.md"), len("A\nB\n"))


class MeasureTest(unittest.TestCase):
    """`measure` -- the roots share one `seen` set, so nothing is double-charged."""

    def test_a_root_imported_by_another_root_is_counted_once(self):
        # This is the real shape: CLAUDE.md's `@AGENTS.md` must not add a second
        # copy of AGENTS.md on top of the one measured as a root in its own right.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("a" * 500 + "\n", encoding="utf-8")
            (root / "CLAUDE.md").write_text("@AGENTS.md\nrule\n", encoding="utf-8")
            with mock.patch.object(check_context_budget, "REPO", root):
                sizes = check_context_budget.measure(("AGENTS.md", "CLAUDE.md"))
        self.assertEqual(sizes, {"AGENTS.md": 501, "CLAUDE.md": len("rule\n")})


class RealFilesTest(unittest.TestCase):
    """The repository's own files are inside the budget."""

    def test_within_budget(self):
        total = sum(check_context_budget.measure().values())
        self.assertLessEqual(
            total,
            check_context_budget.BUDGET,
            f"{total} chars across {check_context_budget.FILES} exceeds the "
            f"{check_context_budget.BUDGET}-char budget; see the module docstring "
            "in check_context_budget.py for what to do about it",
        )

    def test_check_passes(self):
        code, output = run_main()
        self.assertEqual(code, 0)
        self.assertIn("under the", output)


class FailurePathTest(unittest.TestCase):
    """`main()` reports failure loudly.

    Without these, an inverted comparison or a dropped ``return 1`` leaves a
    gate that passes on every input -- and every other test in this file still
    goes green, because they exercise the classifier rather than the verdict.
    """

    def test_over_budget_fails(self):
        with mock.patch.object(check_context_budget, "BUDGET", 100):
            code, output = run_main()
        self.assertEqual(code, 1)
        self.assertIn("FAIL", output)
        # The remedy is in the message, not just the number: a gate that says
        # only "too big" gets answered by deleting a rule.
        self.assertIn("docs/pull-request-workflow.md", output)

    def test_small_overage_is_not_reported_as_zero(self):
        real = sum(check_context_budget.measure().values())
        with mock.patch.object(check_context_budget, "BUDGET", real - 200):
            code, output = run_main()
        self.assertEqual(code, 1)
        self.assertIn("200 over", output)

    def test_missing_file_fails(self):
        with mock.patch.object(check_context_budget, "FILES", ("NOT_A_REAL_FILE.md",)):
            code, output = run_main()
        self.assertEqual(code, 1)
        self.assertIn("MISSING", output)


if __name__ == "__main__":
    unittest.main()
