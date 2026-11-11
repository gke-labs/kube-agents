"""No tracked symlink points outside the repository, and a virtualenv link stays ignored.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, matching the other suites in this directory.

#5387 committed `bench/.venv` as a symlink to one contributor's virtualenv. The smoke
gate extracts `bench/` from the merge target and runs `uv sync` there, which then fails
with "failed to create directory .../bench/.venv: File exists", so every pull request's
smoke run stopped before any eval (#5501). Two things let it in, and each is checked here:

* the link was tracked at all. A symlink whose target is an absolute path, or a relative
  one that climbs out of the tree, resolves only on the machine that wrote it. The
  repository's own links (`.claude/rules`, `.claude/skills`) point into `.agents/`;
* the ignore pattern missed it. `.venv/` with a trailing slash matches a directory only,
  never a symlink, so `git add -A` staged the link. `git check-ignore --no-index` on a path
  that does not exist reads it as a file, which is what a link is to that pattern.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The mode git records for a symbolic link.
SYMLINK_MODE = "120000"
# Virtualenv paths a contributor creates and the gate must never receive.
VENV_PATHS = (".venv", "bench/.venv")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)


class TrackedSymlinksTest(unittest.TestCase):
    def test_every_tracked_symlink_is_relative_and_stays_inside_the_repository(self):
        listing = _git("ls-files", "-s")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        links = [line.split("\t", 1)[1] for line in listing.stdout.splitlines() if line.startswith(SYMLINK_MODE + " ")]
        for path in links:
            with self.subTest(path=path):
                target = _git("cat-file", "-p", f":{path}").stdout
                self.assertFalse(os.path.isabs(target), f"{path} links to an absolute path: {target}")
                resolved = (REPO / path).parent.joinpath(target).resolve()
                self.assertTrue(resolved.is_relative_to(REPO), f"{path} links outside the repository: {target}")

    def test_a_virtualenv_path_is_ignored_whether_it_is_a_directory_or_a_link(self):
        for path in VENV_PATHS:
            with self.subTest(path=path):
                checked = _git("check-ignore", "--no-index", "-q", path)
                self.assertEqual(checked.returncode, 0, f"{path} is not ignored as a file or link; use `.venv`, not `.venv/`")


if __name__ == "__main__":
    unittest.main()
