"""Tests for check_upgrade_table_releases.py, on a small git repository built per test.

    python3 -m unittest scripts.test_check_upgrade_table_releases
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name("check_upgrade_table_releases.py")
PAGE = "docs/site/src/content/docs/install/upgrade.md"
TRACES = "scripts/upgrade_table_traces.txt"
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

    def trace(self, *entries: str) -> None:
        traces = self.root / TRACES
        traces.parent.mkdir(parents=True, exist_ok=True)
        traces.write_text("# a comment\n\n" + "".join(f"{entry}\n" for entry in entries), encoding="utf-8")

    def tag(self, name: str, commit: str = "HEAD") -> None:
        self.git("tag", "-a", name, "-m", name, commit, at=self.tick())

    def reword(self, old: str, new: str) -> None:
        self.rows = [r.replace(f"| {old} |", f"| {new} |") for r in self.rows]
        self.write_page()

    def relabel(self, change: str, release: str) -> None:
        self.rows = [f"| {release} | {change} | x |\n" if f"| {change} |" in r else r for r in self.rows]
        self.write_page()

    def check(self, **env: str) -> tuple[int, str]:
        # The run's own GitHub Actions event is not the one under test.
        clean = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=self.root, capture_output=True, text=True,
                                env={**clean, **env})
        return result.returncode, result.stderr

    def mislabel(self) -> str:
        """A row labelled 1.0.0 that 1.0.0, cut from the commit before it, does not contain."""
        base = self.commit("feat: before the branch point")
        self.commit("fix: b", row=row("1.0.0", "change b"))
        self.tag("1.0.0", base)
        return self.git("rev-parse", "HEAD").strip()

    def pull_request(self, *, page: bool) -> None:
        """Check out the merge commit of a branch that changes the page, or only code."""
        self.git("checkout", "-q", "-b", "feature")
        self.commit("feat: the pull request's change",
                    row=row("9.9.9", "an unreleased change") if page else None)
        self.git("checkout", "-q", "main")
        self.git("checkout", "-q", "-b", "merge")
        self.git("merge", "-q", "--no-ff", "-m", "merge", "feature", at=self.tick())


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

    def test_the_failure_names_the_release_to_relabel_with(self):
        self.repo.mislabel()
        _, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertIn("Relabel it as pending: no release contains it yet.", err)
        self.repo.tag("1.1.0")
        self.repo.tag("1.2.0")
        _, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertIn("Relabel it as 1.1.0, the first release that contains it.", err)

    def test_a_code_change_after_the_cut_labelled_with_that_release_fails(self):
        self.repo.tag("1.0.0")
        self.repo.commit("fix: c", row=row("1.0.0", "change c"))
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('"change c"', err)

    def test_a_reworded_shipped_row_passes_with_a_trace_to_its_first_commit(self):
        first = self.repo.commit("feat: r", row=row("1.0.0", "change r"))
        self.repo.tag("1.0.0")
        self.repo.reword("change r", "change r, reworded")
        self.repo.commit("docs: reword r", code=False)
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn("upgrade_table_traces.txt", err)
        self.repo.trace(f"{first}  change r, reworded")
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))

    def test_a_row_added_after_its_release_passes_with_a_trace_to_the_shipped_change(self):
        shipped = self.repo.commit("feat: l")
        self.repo.tag("1.0.0")
        self.repo.commit("docs: note l", row=row("1.0.0", "change l"), code=False)
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push")[0], 1)
        self.repo.trace(f"{shipped} change l")
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))

    def test_a_trace_to_a_commit_the_release_does_not_contain_fails(self):
        self.repo.tag("1.0.0")
        late = self.repo.commit("feat: l", row=row("1.0.0", "change l"))
        self.repo.trace(f"{late} change l")
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn(f"does not contain {late[:12]}, the commit {TRACES} names for it", err)

    def test_a_trace_to_an_unknown_commit_fails(self):
        self.repo.trace("0123456789abcdef0123456789abcdef01234567 an old change")
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn(f"{TRACES}:3: 0123456789abcdef0123456789abcdef01234567 is not a commit in this history", err)

    def test_a_trace_to_a_commit_outside_this_history_fails(self):
        self.repo.git("checkout", "-q", "-b", "elsewhere")
        other = self.repo.commit("feat: elsewhere")
        self.repo.git("checkout", "-q", "main")
        self.repo.trace(f"{other} an old change")
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn(f"{other} is not a commit in this history or in a release's", err)

    def test_a_trace_to_a_cherry_pick_on_the_release_branch_passes(self):
        base = self.repo.commit("feat: before the branch point")
        self.repo.commit("feat: c on main")
        self.repo.git("checkout", "-q", "-b", "release", base)
        picked = self.repo.commit("feat: c, cherry-picked")
        self.repo.tag("1.0.0")
        self.repo.git("checkout", "-q", "main")
        self.repo.commit("docs: note c", row=row("1.0.0", "change c"), code=False)
        self.repo.trace(f"{picked} change c")
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))

    def test_a_trace_that_is_not_a_full_commit_fails(self):
        self.repo.commit("feat: t", row=row("1.0.0", "change t"))
        self.repo.tag("1.0.0")
        for name in ("1.0.0", "HEAD~1", self.repo.git("rev-parse", "HEAD").strip()[:12]):
            self.repo.trace(f"{name} change t")
            code, err = self.repo.check(GITHUB_EVENT_NAME="push")
            self.assertEqual(code, 1, name)
            self.assertIn("an entry is `<full 40-character commit> <change text>`", err)

    def test_a_second_trace_for_the_same_row_fails(self):
        first = self.repo.commit("feat: d", row=row("1.0.0", "change d"))
        self.repo.tag("1.0.0")
        self.repo.trace(f"{first} change d", f"{first}\tchange d")
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn('a second entry for "change d"', err)

    def test_a_trace_matches_a_cell_with_extra_spaces(self):
        shipped = self.repo.commit("feat: s")
        self.repo.tag("1.0.0")
        self.repo.commit("docs: note s", row="| 1.0.0 |  change s | x |\n", code=False)
        self.repo.trace(f"{shipped} change s")
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))

    def test_an_indented_comment_is_a_comment(self):
        self.repo.trace("   # an indented comment")
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))

    def test_a_trace_with_no_change_text_fails(self):
        head = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.trace(head)
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn("an entry is `<full 40-character commit> <change text>`", err)

    def test_a_trace_whose_row_is_gone_fails(self):
        head = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.trace(f"{head} a row that is not on the page")
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn('no row of the table has the change text "a row that is not on the page"', err)

    def test_a_broken_trace_only_warns_a_change_that_edits_neither_file(self):
        head = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.trace(f"{head} a row that is not on the page")
        base = self.repo.commit("docs: a broken trace", code=False)
        self.repo.git("update-ref", "refs/remotes/origin/main", base)
        self.repo.commit("feat: a local change")
        code, err = self.repo.check()
        self.assertEqual(code, 0)
        self.assertIn("warning: scripts/upgrade_table_traces.txt:3", err)

    def test_a_pull_request_that_edits_only_a_trace_fails_on_the_row(self):
        shipped = self.repo.commit("feat: l")
        self.repo.tag("1.0.0")
        self.repo.commit("docs: note l", row=row("1.0.0", "change l"), code=False)
        self.repo.trace(f"{shipped} change l")
        base = self.repo.commit("docs: trace l", code=False)
        self.repo.git("update-ref", "refs/remotes/origin/main", base)
        late = self.repo.commit("feat: a later change")
        self.repo.trace(f"{late} change l")  # the entry now names a commit 1.0.0 does not contain
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertNotIn("warning:", err)
        (self.repo.root / TRACES).unlink()  # removing the entry is an edit to the row too
        self.assertEqual(self.repo.check()[0], 1)

    def test_a_broken_trace_fails_a_change_that_edits_the_page_or_the_file(self):
        base = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.git("update-ref", "refs/remotes/origin/main", base)
        self.repo.trace(f"{base} a row that is not on the page")
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertNotIn("warning:", err)
        broken = self.repo.commit("docs: a broken trace", code=False)  # now in the base
        self.repo.git("update-ref", "refs/remotes/origin/main", broken)
        self.repo.commit("feat: g", row=row("2.0.0", "change g"))  # this change edits only the page
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn("a row that is not on the page", err)

    def test_a_pending_row_no_release_contains_passes(self):
        self.repo.commit("feat: p", row=row("pending", "change p"))
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))
        code, err = self.repo.check()
        self.assertEqual(code, 0)
        self.assertNotIn("pending", err)

    def test_a_pending_row_a_release_contains_fails_and_names_it(self):
        added = self.repo.commit("feat: p", row=row("pending", "change p"))
        self.repo.tag("1.0.0")
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn(f'the row "change p" is labelled pending, but 1.0.0 contains {added[:12]}', err)
        self.assertIn("Relabel it as 1.0.0.", err)
        self.assertIn("To reword a pending row after its release, label the new text with the release", err)

    def test_a_pending_row_names_the_first_release_in_version_order(self):
        self.repo.commit("feat: p", row=row("pending", "change p"))
        self.repo.tag("1.9.0")
        self.repo.tag("1.10.0")
        _, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertIn("Relabel it as 1.9.0.", err)

    def test_a_pending_row_cherry_picked_into_a_release_fails_and_names_it(self):
        base = self.repo.commit("feat: before the branch point")
        self.repo.commit("feat: p", row=row("pending", "change p"))
        self.repo.git("checkout", "-q", "-b", "release", base)
        self.repo.write_page()  # the same row, cherry-picked to the release branch
        self.repo.commit("feat: p, cherry-picked")
        self.repo.tag("1.0.0")
        self.repo.git("checkout", "-q", "main")
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn("but 1.0.0 contains the row in its own copy of the table. Relabel it as 1.0.0.", err)

    def test_a_pending_row_no_commit_adds_is_reported(self):
        self.repo.tag("1.0.0")
        self.repo.rows.insert(0, row("pending", "change w"))
        self.repo.write_page()  # in the working tree only
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn('no commit in this history adds the row "change w", so its pending label cannot be checked', err)

    def test_a_reworded_pending_row_fails_once_its_release_contains_the_traced_commit(self):
        first = self.repo.commit("feat: q", row=row("pending", "change q"))
        self.repo.tag("1.0.0")
        self.repo.reword("change q", "change q, reworded")
        self.repo.commit("docs: reword q", code=False)
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))  # the known limit: it fails open
        self.repo.trace(f"{first} change q, reworded")
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn(f"but 1.0.0 contains {first[:12]}, the commit {TRACES} names for it. Relabel it as 1.0.0.", err)

    def _shipped_pending_row(self):
        """A `pending` row "change q" that 1.0.0 contains, with origin/main at it; returns its commit."""
        first = self.repo.commit("feat: q", row=row("pending", "change q"))
        self.repo.tag("1.0.0")
        self.repo.git("update-ref", "refs/remotes/origin/main", first)
        return first

    def test_a_change_that_rewords_a_shipped_pending_row_fails(self):
        first = self._shipped_pending_row()
        self.repo.reword("change q", "change q, reworded")
        self.repo.commit("docs: reword q", code=False)  # as on a pull request, HEAD holds the removal
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn(f'this change removes the pending row "change q", which 1.0.0 already contains. If you '
                      f'reworded it, label the new row 1.0.0 and name its commit ({first})', err)
        self.assertIn("relabel it as 1.0.0 first and remove it in a later change", err)

    def test_a_change_that_rewords_an_unshipped_pending_row_passes(self):
        first = self.repo.commit("feat: q", row=row("pending", "change q"))
        self.repo.git("update-ref", "refs/remotes/origin/main", first)
        self.repo.reword("change q", "change q, reworded")
        self.repo.commit("docs: reword q", code=False)
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_change_that_relabels_a_shipped_pending_row_passes(self):
        self._shipped_pending_row()
        self.repo.relabel("change q", "1.0.0")
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_shipped_row_relabelled_first_can_be_removed_later(self):
        self._shipped_pending_row()
        self.repo.relabel("change q", "1.0.0")
        relabelled = self.repo.commit("docs: label q", code=False)
        self.repo.git("update-ref", "refs/remotes/origin/main", relabelled)
        self.repo.rows = [r for r in self.repo.rows if "change q" not in r]
        self.repo.write_page()
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_reword_of_a_shipped_pending_row_recorded_in_the_traces_passes(self):
        first = self._shipped_pending_row()
        self.repo.reword("change q", "change q, reworded")
        self.repo.relabel("change q, reworded", "1.0.0")
        self.repo.trace(f"{first} change q, reworded")
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_trace_for_another_row_does_not_excuse_a_removed_pending_row(self):
        first = self.repo.commit("feat: q and r", row=row("pending", "change r"))
        self.repo.rows.insert(0, row("pending", "change q"))
        self.repo.write_page()
        self.repo.git("commit", "-q", "--amend", "-a", "--no-edit")
        first = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.tag("1.0.0")
        self.repo.reword("change r", "change r2")
        self.repo.relabel("change r2", "1.0.0")
        self.repo.trace(f"{first} change r2")  # main already records the reword of r
        base = self.repo.commit("docs: reword r", code=False)
        self.repo.git("update-ref", "refs/remotes/origin/main", base)
        self.repo.reword("change q", "change q2")  # this change rewords q and records nothing
        self.repo.commit("docs: reword q", code=False)
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('this change removes the pending row "change q"', err)

    def test_one_recorded_reword_excuses_only_one_removed_row(self):
        first = self.repo.commit("feat: q and s", row=row("pending", "change s"))
        self.repo.rows.insert(0, row("pending", "change q"))
        self.repo.write_page()
        self.repo.git("commit", "-q", "--amend", "-a", "--no-edit")
        first = self.repo.git("rev-parse", "HEAD").strip()
        self.repo.tag("1.0.0")
        self.repo.git("update-ref", "refs/remotes/origin/main", first)
        self.repo.reword("change q", "change q2")
        self.repo.relabel("change q2", "1.0.0")
        self.repo.trace(f"{first} change q2")  # q's reword is recorded
        self.repo.reword("change s", "change s2")  # s's reword stays pending and is not
        self.repo.commit("docs: reword q and s", code=False)
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertEqual(err.count("this change removes the pending row"), 1)

    def test_a_reword_labelled_with_a_later_release_is_not_excused(self):
        first = self._shipped_pending_row()
        self.repo.reword("change q", "change q2")
        self.repo.relabel("change q2", "2.0.0")  # no tag yet, so the release-row check reads nothing
        self.repo.trace(f"{first} change q2")
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('this change removes the pending row "change q", which 1.0.0 already contains', err)

    def test_a_reword_recorded_with_the_cherry_pick_passes(self):
        base = self.repo.commit("feat: before the branch point")
        self.repo.commit("feat: q", row=row("pending", "change q"))
        self.repo.git("checkout", "-q", "-b", "release", base)
        self.repo.write_page()
        picked = self.repo.commit("feat: q, cherry-picked")
        self.repo.tag("1.0.0")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.repo.reword("change q", "change q2")
        self.repo.relabel("change q2", "1.0.0")
        self.repo.trace(f"{picked} change q2")
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_push_does_not_read_a_removed_pending_row(self):
        self._shipped_pending_row()
        self.repo.reword("change q", "change q, reworded")
        self.repo.commit("docs: reword q", code=False)
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))  # the stated limit: no base

    def test_a_misspelt_release_cell_fails(self):
        for cell in ("`pending`", "Pending", "next"):
            self.repo.rows = [r for r in self.repo.rows if "change m" not in r]
            self.repo.commit("feat: m", row=row(cell, "change m"))
            code, err = self.repo.check(GITHUB_EVENT_NAME="push")
            self.assertEqual(code, 1, cell)
            self.assertIn(f'the Release cell "{cell}" is neither a release number nor pending', err)

    def test_a_contained_pending_row_warns_a_pull_request_that_leaves_it_alone(self):
        self.repo.commit("feat: p", row=row("pending", "change p"))
        self.repo.tag("1.0.0")
        self.repo.pull_request(page=False)
        code, err = self.repo.check(GITHUB_EVENT_NAME="pull_request")
        self.assertEqual(code, 0)
        self.assertIn('warning: docs/site/src/content/docs/install/upgrade.md', err)
        self.assertIn("labelled pending, but 1.0.0 contains", err)

    def test_a_relabelled_row_passes(self):
        base = self.repo.commit("feat: before the branch point")
        self.repo.commit("fix: b", row=row("1.0.0", "change b"))
        self.repo.tag("1.0.0", base)
        self.repo.relabel("change b", "1.1.0")
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))

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

    def test_an_earlier_mention_in_another_rows_cell_does_not_stand_in_for_the_row(self):
        self.repo.commit("feat: m", row="| 0.9.0 | change m | change k comes later |\n")
        self.repo.tag("1.0.0")
        self.repo.commit("feat: k", row=row("1.0.0", "change k"))
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('"change k"', err)

    def test_a_row_moved_from_prose_in_one_commit_traces_to_that_commit(self):
        self.repo.prose = "Later releases: change k is coming.\n\n"
        self.repo.write_page()
        self.repo.commit("docs: mention change k")
        self.repo.prose = ""
        self.repo.commit("feat: k", row=row("1.0.0", "change k"))
        self.repo.tag("1.0.0")
        self.assertEqual(self.repo.check(), (0, ""))

    def test_removing_a_longer_row_that_holds_the_change_text_leaves_the_row_traced(self):
        self.repo.commit("feat: b", row=row("1.0.0", "change b"))
        self.repo.tag("1.0.0")
        self.repo.commit("feat: b2", row=row("1.1.0", "change b extended"))
        self.repo.rows.remove(row("1.1.0", "change b extended"))
        self.repo.write_page()
        self.repo.commit("revert: b2")
        self.assertEqual(self.repo.check(), (0, ""))

    def test_another_tables_second_cell_does_not_stand_in_for_the_row(self):
        self.repo.prose = "| Key | Where to read it |\n| --- | --- |\n| `K` | change k |\n\n"
        self.repo.write_page()
        self.repo.commit("docs: a key table")
        self.repo.tag("1.0.0")
        self.repo.commit("feat: k", row=row("1.0.0", "change k"))
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('"change k"', err)

    def test_a_row_added_as_pending_and_labelled_after_the_cut_traces_to_its_first_commit(self):
        self.repo.commit("feat: p", row=row("pending", "change p"))
        self.repo.tag("1.0.0")
        self.repo.relabel("change p", "1.0.0")
        self.repo.commit("docs: label the 1.0.0 rows", code=False)
        self.assertEqual(self.repo.check(), (0, ""))

    def test_a_row_reverted_before_the_cut_and_relanded_after_it_fails(self):
        self.repo.commit("feat: b", row=row("1.0.0", "change b"))
        self.repo.rows.remove(row("1.0.0", "change b"))
        self.repo.write_page()
        self.repo.commit("revert: b")
        self.repo.tag("1.0.0")
        self.repo.commit("feat: b again", row=row("1.0.0", "change b"))
        code, err = self.repo.check()
        self.assertEqual(code, 1)
        self.assertIn('"change b"', err)

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
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))

    def test_a_local_run_notes_the_releases_it_has_no_tag_for(self):
        self.repo.commit("feat: g", row=row("2.0.0", "change g"))
        code, err = self.repo.check()
        self.assertEqual(code, 0)
        self.assertIn("note: rows labelled 2.0.0 are not checked", err)
        self.assertNotIn("note:", self.repo.check(GITHUB_EVENT_NAME="push")[1])

    def test_the_real_page_has_rows_the_check_reads(self):
        page = (SCRIPT.parents[1] / PAGE).read_text(encoding="utf-8")
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            import check_upgrade_table_releases as check
        finally:
            sys.path.remove(str(SCRIPT.parent))
        table = page.split(HEADING.strip(), 1)[1].split("\n#", 1)[0]
        lines = [line for line in table.splitlines() if line.startswith("| ") and "---" not in line][1:]
        self.assertTrue(lines, "the breaking-changes table has rows")
        self.assertEqual(len(check.rows(table)), len(lines), "every row of the real table is read")

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

    def test_a_pull_request_that_adds_a_wrong_label_fails(self):
        base = self.repo.commit("feat: before the branch point")
        self.repo.tag("1.0.0", base)
        self.repo.git("checkout", "-q", "-b", "feature")
        self.repo.commit("fix: b", row=row("1.0.0", "change b"))
        self.repo.git("checkout", "-q", "main")
        self.repo.git("checkout", "-q", "-b", "merge")
        self.repo.git("merge", "-q", "--no-ff", "-m", "merge", "feature", at=self.repo.tick())
        code, err = self.repo.check(GITHUB_EVENT_NAME="pull_request")
        self.assertEqual(code, 1)
        self.assertIn('"change b"', err)
        self.assertNotIn("warning:", err)

    def test_a_pull_request_that_relabels_a_row_wrongly_fails(self):
        self.repo.mislabel()
        self.repo.git("checkout", "-q", "-b", "feature")
        self.repo.relabel("change b", "0.9.0")
        self.repo.commit("docs: relabel b", code=False)
        self.repo.git("checkout", "-q", "main")
        self.repo.git("checkout", "-q", "-b", "merge")
        self.repo.git("merge", "-q", "--no-ff", "-m", "merge", "feature", at=self.repo.tick())
        code, err = self.repo.check(GITHUB_EVENT_NAME="pull_request")
        self.assertEqual(code, 1)
        self.assertIn('"change b" is labelled 0.9.0', err)

    def test_a_pull_request_that_changes_another_part_of_the_page_only_warns(self):
        self.repo.mislabel()
        self.repo.pull_request(page=True)
        code, err = self.repo.check(GITHUB_EVENT_NAME="pull_request")
        self.assertEqual(code, 0)
        self.assertIn('warning: docs/site/src/content/docs/install/upgrade.md', err)
        self.assertIn("does not add or relabel these rows", err)

    def test_a_pull_request_that_leaves_the_page_alone_only_warns(self):
        self.repo.mislabel()
        self.repo.pull_request(page=False)
        code, err = self.repo.check(GITHUB_EVENT_NAME="pull_request")
        self.assertEqual(code, 0)
        self.assertIn('warning: docs/site/src/content/docs/install/upgrade.md', err)
        self.assertIn("main's push run fails until a pull request relabels them", err)

    def test_a_push_to_main_fails_on_a_wrong_label(self):
        head = self.repo.mislabel()
        # As in CI, origin/main is the pushed commit itself: the page did not change against it.
        self.repo.git("update-ref", "refs/remotes/origin/main", head)
        code, err = self.repo.check(GITHUB_EVENT_NAME="push")
        self.assertEqual(code, 1)
        self.assertIn('"change b"', err)

    def test_a_merge_group_that_leaves_the_page_alone_only_warns(self):
        base = self.repo.mislabel()
        self.repo.commit("feat: the group's change")
        event = Path(self.tmp.name) / "event.json"
        event.write_text(json.dumps({"merge_group": {"base_sha": base}}), encoding="utf-8")
        code, err = self.repo.check(GITHUB_EVENT_NAME="merge_group", GITHUB_EVENT_PATH=str(event))
        self.assertEqual((code, "warning:" in err), (0, True))

    def test_a_local_run_compares_with_origin_main(self):
        base = self.repo.mislabel()
        self.repo.git("update-ref", "refs/remotes/origin/main", base)
        self.repo.commit("feat: a local change")
        code, err = self.repo.check()
        self.assertEqual((code, "warning:" in err), (0, True))
        self.repo.relabel("change b", "0.9.0")  # a relabel this change makes: the label fails
        code, _ = self.repo.check()
        self.assertEqual(code, 1)

    def test_a_shallow_clone_fails_even_where_a_wrong_label_would_only_warn(self):
        self.repo.commit("feat: i")
        clone = Path(self.tmp.name) / "shallow-pr"
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--no-local", str(self.repo.root), str(clone)],
                       check=True, capture_output=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=clone, capture_output=True, text=True,
                                env={**env, "GITHUB_EVENT_NAME": "pull_request"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("shallow", result.stderr)

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
