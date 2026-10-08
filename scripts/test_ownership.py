#!/usr/bin/env python3
"""Keep docs/ownership.md in step with the tree and with OWNERS.

Run: cd scripts && python3 -m unittest test_ownership

The ownership page is hand-written, and the three ways it rots are all silent:
a directory is renamed and its row still names the old path, a person leaves
and their login stays as an area's primary, or a new top-level entry lands in
AGENTS.md's Repository Layout that no area's key paths include. Each of those reads as
a complete page to the next person who opens it.
"""

import re
import sys
import unittest
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# The same answer to "is this path in the tree" that the docs link checker
# gives: tracked by git, not merely present on disk, so a generated or ignored
# file is not in it.
from check_docs_links import tracked_paths

REPO = _HERE.parent
OWNERSHIP_DOC = REPO / "docs" / "ownership.md"
OWNERS_FILE = REPO / "OWNERS"
AGENTS_FILE = REPO / "AGENTS.md"

# The one table, by the heading it sits under.
TABLE_HEADING = "## Areas, services and roles"
# The AGENTS.md section whose top-level `- `path`:` bullets must each have an area row.
LAYOUT_HEADING = "## Repository Layout"

# Column positions: the area, its key paths, then the two people.
COL_SUBJECT = 0
COL_PATHS = 1
COL_PRIMARY = 2
COL_BACKUP = 3
# A primary that is honestly nobody; the page explains it.
UNOWNED = "unowned"
# The only person cells that are not a login. Closed on purpose: a cell with a
# space in it would otherwise be a place to write a login the test never checks.
PROSE_FALLBACKS = ("the owner of the area it designs, or its `Author:` line where one exists",)

BACKTICKED_RE = re.compile(r"`([^`]+)`")
# A backticked token in a prose cell that the page means as a repository path:
# relative, with a directory component or one of the extensions the tree uses,
# and no glob or field punctuation (`gke-*`, `owner:`). Versions (`3.9.6`),
# abbreviations (`e.g.`) and slash commands (`/review`) are not paths.
PROSE_PATH_RE = re.compile(r"^(?!\.\.?(/|$))(?!/)[\w.-]+(/[\w.-]*)+$|^[\w-]+\.(md|py|sh|yaml|yml|json|go|txt|env)$")
# A top-level Repository Layout bullet: no indent; the backticked paths before
# the first colon are the entries. Any top-level bullet with none is a shape
# the test does not read and fails on, rather than one it silently skips.
LAYOUT_BULLET_PREFIX = "- "
LAYOUT_ENTRY_SEPARATOR = ":"


def _section(path, heading):
    """The lines of `path` under `heading`, up to the next heading of the same level."""
    lines = path.read_text().splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError:
        raise AssertionError(f"{path.relative_to(REPO)} has no {heading!r} heading")
    body = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return body


def _table_rows(lines):
    """Body rows of the first Markdown table in `lines`, as lists of stripped cells."""
    rows = [line for line in lines if line.startswith("|")]
    # Drop the header row and the `| --- |` separator under it.
    body = rows[2:]
    return [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in body]


def _owners_logins():
    """Lower-cased, as scripts/request_reviewers.py reads them: GitHub logins are case-insensitive."""
    data = yaml.safe_load(OWNERS_FILE.read_text())
    return {login.lower() for login in data.get("approvers", []) + data.get("reviewers", [])}


def _layout_entries_in(lines):
    """Paths named by the top-level bullets in a Repository Layout section."""
    entries, unreadable = [], []
    for line in lines:
        if not line.startswith(LAYOUT_BULLET_PREFIX):
            continue
        tokens = BACKTICKED_RE.findall(line.split(LAYOUT_ENTRY_SEPARATOR, 1)[0])
        if not tokens:
            unreadable.append(line)
        entries.extend(tokens)
    if unreadable:
        raise AssertionError(
            "Repository Layout bullets with no backticked path before the colon; "
            f"the coverage check cannot read them: {unreadable}"
        )
    return entries


def _layout_entries():
    return _layout_entries_in(_section(AGENTS_FILE, LAYOUT_HEADING))


def _is_prose_path(token):
    return bool(PROSE_PATH_RE.match(token))


_TRACKED = None


def _tracked():
    global _TRACKED
    if _TRACKED is None:
        _TRACKED = tracked_paths()
    return _TRACKED


def _in_tree(path):
    """True when `path` is a tracked file, or a directory some tracked file sits under."""
    target = (REPO / path).resolve()
    if not target.is_relative_to(REPO):
        return False
    return target in _tracked() or any(target in tracked.parents for tracked in _tracked())


class OwnershipDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _table_rows(_section(OWNERSHIP_DOC, TABLE_HEADING))
        cls.logins = _owners_logins()
        cls.layout = _layout_entries()
        for name, value in (
            ("ownership table", cls.rows),
            ("OWNERS", cls.logins),
            ("AGENTS.md Repository Layout", cls.layout),
        ):
            if not value:
                raise AssertionError(f"{name} is empty; nothing to check")

    def _paths(self):
        """Every path the table names: all of the key-paths column, and the
        path-shaped tokens of the other cells."""
        for row in self.rows:
            for column, cell in enumerate(row):
                for token in BACKTICKED_RE.findall(cell):
                    if column == COL_PATHS or _is_prose_path(token):
                        yield row[COL_SUBJECT], token

    def test_every_path_exists(self):
        paths = list(self._paths())
        self.assertTrue(paths, "the table names no paths at all; the scan is broken, not the page")
        missing = [f"{subject}: {path}" for subject, path in paths if not _in_tree(path)]
        self.assertEqual(missing, [], "the page names paths that are not in the tree")

    def test_every_layout_entry_has_its_own_row(self):
        key_paths = {token for row in self.rows for token in BACKTICKED_RE.findall(row[COL_PATHS])}
        uncovered = [entry for entry in self.layout if entry not in key_paths]
        self.assertEqual(
            uncovered, [], "AGENTS.md Repository Layout entries that are no area's key path"
        )

    def _check_person(self, cell, subject, column, problems):
        if cell in ("", UNOWNED) or cell in PROSE_FALLBACKS:
            return
        if cell.lower() not in self.logins:
            problems.append(
                f"{subject} / {column}: {cell!r} is not a login in OWNERS "
                f"(one login per cell, {UNOWNED!r}, or a fallback from PROSE_FALLBACKS)"
            )

    def test_every_person_is_in_owners(self):
        problems = []
        for subject, primary, backup in self._people():
            self._check_person(primary, subject, "primary", problems)
            self._check_person(backup, subject, "backup", problems)
        self.assertEqual(problems, [])

    def _people(self):
        for row in self.rows:
            yield row[COL_SUBJECT], row[COL_PRIMARY], row[COL_BACKUP]

    def test_primary_is_never_blank(self):
        blank = [subject for subject, primary, _ in self._people() if primary == ""]
        self.assertEqual(blank, [], f"rows with no primary (write {UNOWNED!r} if that is the truth)")


class HeuristicsTest(unittest.TestCase):
    """The shapes the table scan reads and the ones it must not."""

    def test_prose_path_shapes(self):
        for token in ("docs/ci-health.md", "scripts/release/", "bench/tf/fleet/", "OWNERS.md", "a.yaml"):
            self.assertTrue(_is_prose_path(token), token)
        for token in ("e.g.", "3.9.6", "/review", "/tmp/x", "../kube-agents-bot/", "./x", "gke-*", "owner:", "OWNERS"):
            self.assertFalse(_is_prose_path(token), token)

    def test_in_tree_means_tracked(self):
        self.assertTrue(_in_tree("AGENTS.md"))
        self.assertTrue(_in_tree("docs/"))
        self.assertFalse(_in_tree("/tmp"))
        self.assertFalse(_in_tree("../"))
        self.assertFalse(_in_tree("no/such/path"))
        # Present on disk in a working clone, ignored by git: not in the tree.
        self.assertFalse(_in_tree(".git/"))

    def test_layout_entries_read_every_bullet_shape(self):
        lines = [
            "- `agents/`: Source of truth.",
            "  - `chat/`: nested, not top level.",
            "- `hack/` (CI scripts): with an aside.",
            "- `tests/`, `bench/`: two in one bullet.",
            "",
        ]
        self.assertEqual(_layout_entries_in(lines), ["agents/", "hack/", "tests/", "bench/"])
        with self.assertRaises(AssertionError):
            _layout_entries_in(["- hack/: no backticks."])


if __name__ == "__main__":
    sys.exit(unittest.main())
