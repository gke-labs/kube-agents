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
        self.assertIn("Relabel a pending row before you reword it", err)

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
