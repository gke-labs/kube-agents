#!/usr/bin/env python3
"""Tests for check_docs_links.py.

Two kinds of test here, and the split is deliberate.

The synthetic ones build a miniature git repository in a temporary directory
and point the checker at it. They prove each rule *fires*: a lint nobody has
watched fail is a lint nobody knows is wired up.

The rest run against the real repository. They do not re-run the lint over the
tree -- `make docs-check-links` already does that in CI, and a second copy of
its verdict would only make one dangling citation red two jobs. They pin what
would silently turn the scan into a no-op: a glob that stops matching the
files it exists for, and this module growing a literal it would then report.
A lint that stops reporting anything looks exactly like a clean repository.

Every design-document path a fixture cites is assembled from DESIGNS and
ARCHITECTURE at runtime. This file is itself a tracked ``.py``, so a literal
``docs/designs/<name>.md`` written here would be scanned and reported as a
broken citation of a document that exists only in a temporary directory.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import check_docs_links as cdl

REPO = Path(__file__).resolve().parents[1]

# Directory prefixes the scan is anchored on, kept apart from the file names so
# no line of this module is itself a citation (see the module docstring).
DESIGNS = "docs/designs/"
ARCHITECTURE = "docs/architecture/"

PRESENT = DESIGNS + "present.md"
MISSING = DESIGNS + "missing.md"
ON_DISK_ONLY = DESIGNS + "on_disk_only.md"
MISSING_PLAIN = "docs/missing.md"  # outside the scanned directories: a Markdown-link fixture only

# Files the scan has to keep reaching: the two #992 was filed against, and one
# of each other kind CODE_GLOBS names, each of which cites a design document.
MUST_REACH = (
    "agents/platform/scripts/cluster_agent_reconcile.py",
    "agents/platform/scripts/cluster_agent_profile.py",
    "deploy/sandbox/Dockerfile",
    "a2a/cmd/gateway/main.go",
    "charts/kube-agents/values.yaml",
    "hack/ci-eval-pr.sh",
)

TOOL_LINES = (
    "# tool.py",
    f"# see {PRESENT} §4).",
    f"# see {MISSING}",
    f'print("`{ON_DISK_ONLY}`")',
    f"# {PRESENT}#section, {PRESENT}:12",
)
README_LINES = (
    "# fixture",
    f"[ok]({PRESENT})",
    f"[bad]({MISSING_PLAIN})",
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@unittest.skipUnless(shutil.which("git"), "the checker shells out to git")
class SyntheticRepoTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Resolved, because tracked_paths() resolves and /tmp may be a symlink.
        self.root = Path(self._tmp.name).resolve()
        _git(self.root, "init", "-q")
        _write(self.root, PRESENT, "# present\n")
        _write(self.root, ON_DISK_ONLY, "# not added\n")
        self.readme = _write(self.root, "README.md", "\n".join(README_LINES) + "\n")
        self.tool = _write(self.root, "tool.py", "\n".join(TOOL_LINES) + "\n")
        _git(self.root, "add", PRESENT, "README.md", "tool.py")
        patcher = mock.patch.object(cdl, "REPO", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reports_a_dangling_citation_with_its_line(self) -> None:
        problems = cdl.check_code_file(self.tool, cdl.tracked_paths())
        self.assertEqual(
            problems,
            [
                f"tool.py:3: broken citation -> {MISSING}",
                f"tool.py:4: broken citation -> {ON_DISK_ONLY}",
            ],
        )

    def test_trailing_punctuation_anchor_and_line_ref_are_not_the_path(self) -> None:
        problems = cdl.check_code_file(self.tool, cdl.tracked_paths())
        self.assertFalse([p for p in problems if ":2:" in p or ":5:" in p], problems)

    def test_markdown_links_are_still_checked(self) -> None:
        problems = cdl.check_file(self.readme, cdl.tracked_paths())
        self.assertEqual(problems, [f"README.md:3: broken link -> {MISSING_PLAIN}"])

    def test_main_reports_both_kinds_and_exits_one(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cdl.main()
        self.assertEqual(rc, 1)
        # README.md plus the present design doc are the tracked Markdown; tool.py is the code.
        self.assertIn("2 Markdown files and design-doc citations in 1 code files", out.getvalue())
        self.assertEqual(err.getvalue().count("broken link ->"), 1)
        self.assertEqual(err.getvalue().count("broken citation ->"), 2)

    def test_main_is_clean_once_every_citation_is_tracked(self) -> None:
        _write(self.root, MISSING, "# now present\n")
        _write(self.root, MISSING_PLAIN, "# now present\n")
        _git(self.root, "add", MISSING, ON_DISK_ONLY, MISSING_PLAIN)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cdl.main()
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("All relative links and design-doc citations resolve.", out.getvalue())


class CitationPatternTest(unittest.TestCase):
    def test_mdx_glob_and_placeholder_do_not_match(self) -> None:
        for text in (
            DESIGNS + "page.mdx",
            DESIGNS + "*.md",
            DESIGNS + "{name}.md",
            "docs/site/src/content/docs/deploy/x.md",
        ):
            with self.subTest(text=text):
                self.assertIsNone(cdl.CITATION_RE.search(text))

    def test_dotted_name_and_both_directories_match_whole(self) -> None:
        dotted = DESIGNS + "a.b.md"
        security = ARCHITECTURE + "03-security-model.md"
        memory = DESIGNS + "memory.md"
        for text, expected in (
            (f"see {dotted}.", dotted),
            (f"({security}#x)", security),
            (f"`{memory}`", memory),
        ):
            with self.subTest(text=text):
                m = cdl.CITATION_RE.search(text)
                self.assertIsNotNone(m)
                self.assertEqual(m.group(0), expected)


class RealRepoTest(unittest.TestCase):
    def test_the_scan_reaches_the_files_it_exists_for(self) -> None:
        code = {p.relative_to(REPO).as_posix() for p in cdl.tracked_code()}
        for rel in MUST_REACH:
            with self.subTest(file=rel):
                self.assertIn(rel, code)

    def test_this_module_contains_no_literal_citation(self) -> None:
        """The guard behind the module docstring: a literal here would fail docs-check."""
        problems = cdl.check_code_file(Path(__file__).resolve(), cdl.tracked_paths())
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
