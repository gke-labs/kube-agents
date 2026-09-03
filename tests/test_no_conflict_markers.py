"""No tracked file carries an unresolved merge-conflict marker.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

A botched rebase resolution reached INSTALL.md: the conflict markers and both
halves of the conflict were committed, so the published install guide told a
reader that re-runs rebuild `k8s-operator/scripts/vars.sh` and, seven lines
below, that they load `install.env`. Nothing in the suite noticed. `make
docs-check` passes over conflict markers — it checks generated regions, links,
terminology, map coverage and the context budget, none of which look at the
line. The only thing that caught it was CI's prettier job, which reformats the
stray `=======` rather than naming it, and which does not run on `.sh`, `.py`
or `.go` at all.

The marker prefixes are matched at a line start and followed by a space or a
line end, which is what git writes. That keeps a Markdown `-------` table rule
and a Python `====` banner comment out of it, and keeps this file's own
docstring from matching itself.
"""

import pathlib
import re
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# `git write-tree`-style markers only: 7 characters, at a line start, followed
# by a space (the `<<<<<<< HEAD` / `>>>>>>> commit` forms) or the end of the
# line (the bare `=======` separator).
_MARKER = re.compile(r"^(?:<{7}|>{7}|={7})(?: |$)", re.MULTILINE)


def _tracked_files():
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


class NoConflictMarkersTest(unittest.TestCase):
    def test_no_tracked_file_has_conflict_markers(self):
        offenders = []
        for rel in _tracked_files():
            path = _REPO_ROOT / rel
            # This file documents the markers it looks for.
            if path == pathlib.Path(__file__).resolve():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                continue  # binary blob or a symlink into nothing
            for match in _MARKER.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{rel}:{line_no}: {match.group(0).rstrip()}")
        self.assertEqual(
            [],
            offenders,
            "unresolved merge-conflict markers are committed:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
