"""Tests for relabel_upgrade_table_pending.py, on the small git repository the check's tests build.

    python3 -m unittest scripts.test_relabel_upgrade_table_pending
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_check_upgrade_table_releases import PAGE, Repo, row  # noqa: E402

SCRIPT = Path(__file__).resolve().with_name("relabel_upgrade_table_pending.py")


def relabel(repo: Repo) -> tuple[int, str, str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo.root, capture_output=True, text=True, env=clean)
    return result.returncode, result.stdout, result.stderr


def page(repo: Repo) -> str:
    return (repo.root / PAGE).read_text(encoding="utf-8")


class RelabelUpgradeTablePendingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repo(Path(self.tmp.name))
        self.repo.commit("start", row=row("0.9.0", "an old change"))
        self.repo.tag("0.9.0")

    def test_a_pending_row_the_new_tag_contains_is_relabelled_and_one_it_does_not_stays(self):
        self.repo.commit("feat: shipped", row=row("pending", "a shipped change"))
        self.repo.tag("1.0.0")
        self.repo.commit("feat: not yet", row=row("pending", "an unshipped change"))
        code, out, err = relabel(self.repo)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "1.0.0\ta shipped change\n")
        text = page(self.repo)
        self.assertIn("| 1.0.0   | a shipped change | x |", text)
        self.assertIn("| pending | an unshipped change | x |", text)

    def test_the_check_accepts_what_the_relabel_writes(self):
        self.repo.commit("feat: shipped", row=row("pending", "a shipped change"))
        self.repo.tag("1.0.0")
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push")[0], 1)
        relabel(self.repo)
        self.repo.commit("docs: relabel", code=False)
        self.assertEqual(self.repo.check(GITHUB_EVENT_NAME="push"), (0, ""))

    def test_nothing_to_relabel_leaves_the_page_alone_and_prints_nothing(self):
        self.repo.commit("feat: not yet", row=row("pending", "an unshipped change"))
        before = page(self.repo)
        self.assertEqual(relabel(self.repo), (0, "", ""))
        self.assertEqual(page(self.repo), before)

    def test_a_row_an_earlier_release_already_contains_takes_that_release(self):
        """A row a release shipped but nobody relabelled takes the first release that contains it."""
        self.repo.commit("feat: shipped", row=row("pending", "a missed change"))
        self.repo.tag("1.0.0")
        self.repo.commit("feat: later")
        self.repo.tag("1.1.0")
        self.assertEqual(relabel(self.repo)[1], "1.0.0\ta missed change\n")

    def test_a_label_wider_than_the_cell_keeps_a_space_before_the_pipe(self):
        self.repo.commit("feat: shipped", row=row("pending", "a shipped change"))
        self.repo.tag("10.10.10")
        relabel(self.repo)
        self.assertIn("| 10.10.10 | a shipped change | x |", page(self.repo))

    def test_a_traced_row_a_release_holds_only_by_cherry_pick_stays_pending_with_a_warning(self):
        """The check reads a labelled traced row by its commit alone, so a label from the release's own
        table would fail it: the row is left for a person."""
        base = self.repo.git("rev-parse", "HEAD").strip()
        shipped = self.repo.commit("feat: c", row=row("pending", "a cherry-picked change"))
        self.repo.git("checkout", "-q", "-b", "release-line", base)
        self.repo.git("cherry-pick", shipped)
        self.repo.tag("1.0.0")
        self.repo.git("checkout", "-q", "main")
        self.repo.trace(f"{shipped} a cherry-picked change")
        code, out, err = relabel(self.repo)
        self.assertEqual((code, out), (0, ""))
        self.assertIn("left pending:", err)
        self.assertIn("| pending | a cherry-picked change | x |", page(self.repo))

    def test_a_shallow_clone_is_refused(self):
        self.repo.commit("feat: shipped", row=row("pending", "a shipped change"))
        shallow = Path(self.tmp.name) / "shallow"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.repo.root}", str(shallow)],
                       check=True, capture_output=True)
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=shallow, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("shallow", result.stderr)


if __name__ == "__main__":
    unittest.main()
