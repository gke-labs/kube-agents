"""Tests for check_upgrade_table_releases.py, on a small git repository built per test.

    python3 -m unittest scripts.test_check_upgrade_table_releases
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("check_upgrade_table_releases.py")
PAGE = "docs/site/src/content/docs/install/upgrade.md"
HEADING = "### Breaking changes in recent releases\n\n"
HEADER = "| Release | Change | What an existing install meets |\n| --- | --- | --- |\n"


class Repo:
    """A repository whose commits and tags carry the times a test gives them."""

    def __init__(self, root: Path):
        self.root = root
        self.clock = 1_700_000_000
        self.rows: list[str] = []
        self.prose = ""
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")

    def git(self, *args: str, at: int | None = None) -> str:
        env = dict(os.environ)
        if at is not None:
            stamp = f"@{at} +0000"
            env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
        return subprocess.run(["git", *args], cwd=self.root, env=env, check=True,
                              capture_output=True, text=True).stdout

    def tick(self) -> int:
        self.clock += 60
        return self.clock

    def write_page(self) -> None:
        page = self.root / PAGE
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text("# Upgrade\n\n" + self.prose + HEADING + HEADER + "".join(self.rows), encoding="utf-8")

    def commit(self, message: str, *, row: str | None = None, code: bool = True) -> str:
        if row is not None:
            self.rows.insert(0, row)
            self.write_page()
        if code:
            code_file = self.root / "src" / "change.txt"
            code_file.parent.mkdir(exist_ok=True)
            code_file.write_text(message, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, at=self.tick())
        return self.git("rev-parse", "HEAD").strip()

    def tag(self, name: str, commit: str = "HEAD") -> None:
        self.git("tag", "-a", name, "-m", name, commit, at=self.tick())

    def reword(self, old: str, new: str) -> None:
        self.rows = [r.replace(f"| {old} |", f"| {new} |") for r in self.rows]
        self.write_page()

    def relabel(self, change: str, release: str) -> None:
        self.rows = [f"| {release} | {change} | x |\n" if f"| {change} |" in r else r for r in self.rows]
        self.write_page()

    def check(self) -> tuple[int, str]:
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.root, capture_output=True, text=True)
        return result.returncode, result.stderr


def row(release: str, change: str) -> str:
    return f"| {release} | {change} | x |\n"


class CheckUpgradeTableReleasesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repo(Path(self.tmp.name))
        self.repo.commit("start", row=row("0.9.0", "an old change"))
        self.repo.tag("0.9.0")

    def test_a_row_its_release_contains_passes(self):
        self.repo.commit("feat: a", row=row("1.0.0", "change a"))
        self.repo.tag("1.0.0")
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_row_committed_before_the_cut_and_left_out_of_it_fails(self):
        base = self.repo.commit("feat: before the branch point")
        self.repo.commit("fix: b", row=row("1.0.0", "change b"))
        self.repo.tag("1.0.0", base)  # cut from the earlier commit, after `change b` merged
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('the row "change b" is labelled 1.0.0, but 1.0.0 does not contain', err)

    def test_a_code_change_after_the_cut_labelled_with_that_release_fails(self):
        self.repo.tag("1.0.0")
        self.repo.commit("fix: c", row=row("1.0.0", "change c"))
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('"change c"', err)

    def test_a_relabelled_row_passes(self):
        base = self.repo.commit("feat: before the branch point")
        self.repo.commit("fix: b", row=row("1.0.0", "change b"))
        self.repo.tag("1.0.0", base)
        self.repo.relabel("change b", "1.1.0")
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_row_cherry_picked_to_the_release_branch_passes(self):
        base = self.repo.commit("feat: before the branch point")
        self.repo.commit("fix: d", row=row("1.0.1", "change d"))
        self.repo.git("checkout", "-q", "-b", "release/1.0", base)
        self.repo.git("cherry-pick", "-x", "main", at=self.repo.tick())
        self.repo.tag("1.0.1")
        self.repo.git("checkout", "-q", "main")
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_row_for_a_release_tagged_before_the_table_existed_is_not_checked(self):
        root = Path(self.tmp.name) / "older"
        root.mkdir()
        repo = Repo(root)
        repo.commit("feat: before the table")
        repo.tag("0.5.0")
        repo.commit("docs: start the table, listing 0.5.0", row=row("0.5.0", "change e"), code=False)
        self.assertEqual(repo.check(), (0, ""))

    def test_a_documentation_only_row_naming_a_release_cut_before_it_fails(self):
        self.repo.tag("1.0.0")
        self.repo.commit("docs: label the change 1.0.0", row=row("1.0.0", "change e"), code=False)
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('"change e"', err)

    def test_an_earlier_mention_in_prose_does_not_stand_in_for_the_row(self):
        self.repo.prose = "Later releases: change k is coming.\n\n"
        self.repo.write_page()
        self.repo.commit("docs: mention change k")
        self.repo.tag("1.0.0")
        self.repo.commit("feat: k", row=row("1.0.0", "change k"))
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('"change k"', err)

    def test_a_row_whose_cell_later_lost_a_prefix_traces_to_its_first_commit(self):
        self.repo.commit("feat: j", row=row("1.0.0", "#12: change j"))
        self.repo.tag("1.0.0")
        self.repo.reword("#12: change j", "change j")
        self.repo.commit("docs: drop the pull-request prefixes", code=False)
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_documentation_only_row_written_before_the_cut_and_left_out_fails(self):
        base = self.repo.commit("feat: before the branch point")
        self.repo.commit("docs: expect f in 1.0.0", row=row("1.0.0", "change f"), code=False)
        self.repo.tag("1.0.0", base)
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('"change f"', err)

    def test_a_row_for_a_release_with_no_tag_is_not_checked(self):
        self.repo.commit("feat: g", row=row("2.0.0", "change g"))
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_row_no_commit_adds_is_reported(self):
        self.repo.tag("1.0.0")
        self.repo.rows.insert(0, row("1.0.0", "change h"))
        self.repo.write_page()  # in the working tree only
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('no commit in this history adds the row "change h"', err)

    def test_a_clone_with_no_release_tag_fails(self):
        self.repo.git("tag", "-d", "0.9.0")
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn("no release tag", err)

    def test_a_shallow_clone_fails(self):
        self.repo.commit("feat: i")
        clone = Path(self.tmp.name) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--no-local", str(self.repo.root), str(clone)],
                       check=True, capture_output=True)
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=clone, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("shallow", result.stderr)


if __name__ == "__main__":
    unittest.main()
