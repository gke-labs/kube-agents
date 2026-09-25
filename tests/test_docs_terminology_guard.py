"""The terminology guard's failure modes that a green tree cannot show.

`hack/check-docs-terminology.sh` reads the repository it ships in — `git
ls-files`, `install.defaults.env`, `audit_report.py` — and none of that is
fixtured, so these cases run the real script and read its verdict. It accepts
extra documents in `DOCS_TERMINOLOGY_EXTRA_FILES`, appended to the tracked set,
so a path built to fail one check runs against the real sources and the
assertion is on the error the check prints, not on the exit status alone: a
terminology failure elsewhere in the tree must not read as one of these.

Each case here is a way the guard once reported green while checking nothing:
invoked from a directory other than the root, handed a file grep could not
read, or diagnosing a missing copy of a cap when the search for it had failed.

Run:
  python3 -m unittest discover -s tests -p 'test_docs_terminology_guard.py' -v
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD = REPO_ROOT / "hack" / "check-docs-terminology.sh"


def run_guard(
    documents: list[str], extra_paths: tuple[str, ...] = ()
) -> subprocess.CompletedProcess:
    """Run the guard over the real tree plus the given documents."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = []
        for index, document in enumerate(documents):
            doc = root / f"doc{index}.md"
            doc.write_text(textwrap.dedent(document).lstrip("\n"), encoding="utf-8")
            paths.append(str(doc))
        paths.extend(extra_paths)
        listing = root / "extra-files.txt"
        listing.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
        return subprocess.run(
            [str(GUARD)],
            cwd=REPO_ROOT,
            env={**os.environ, "DOCS_TERMINOLOGY_EXTRA_FILES": str(listing)},
            capture_output=True,
            text=True,
            check=False,
        )


class GuardVerdictTest(unittest.TestCase):
    """What the script reports when a check cannot run."""

    UNRUN = "A terminology check could not run"

    def test_a_file_grep_cannot_read_is_a_check_that_did_not_run(self):
        # Folded into an empty result by `2>/dev/null || true`, an unreadable
        # file was a check that passed. It is reported by name, and the copy
        # floors -- which would otherwise each announce that their cap is
        # undocumented -- stay silent, because the search did not run.
        missing = str(REPO_ROOT / "docs" / "this-file-does-not-exist.md")
        proc = run_guard([], extra_paths=(missing,))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(self.UNRUN, proc.stdout)
        self.assertIn("this-file-does-not-exist.md", proc.stdout)
        self.assertNotIn("No document states this cap", proc.stdout)


class GuardWiringTest(unittest.TestCase):
    """Properties of the script a source read or a run from elsewhere shows."""

    def test_the_guard_runs_from_a_directory_that_is_not_the_repository_root(self):
        # The script cd's to the repository root at the top; a helper it once
        # resolved against `$0` was still relative to the *caller's* directory,
        # so `cd hack && ./check-docs-terminology.sh` exited 1 on "not found"
        # before checking a single identifier, while CI, which runs it from the
        # root, stayed green. A source read cannot see that, so this executes.
        # Invoked by a *relative* path, which is how a person types it and the
        # only spelling that reproduces the break: `dirname` on an absolute
        # `$0` lands on the right directory from any cwd, so a test that runs
        # the script by its full path passes either way.
        for cwd in (REPO_ROOT / "hack", REPO_ROOT / "docs"):
            with self.subTest(cwd=cwd.name):
                proc = subprocess.run(
                    ["./" + os.path.relpath(GUARD, cwd)],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotIn("cannot run", proc.stdout + proc.stderr)
                self.assertNotIn("could not run", proc.stdout + proc.stderr)
                # Not the exit status: that would make this suite a second
                # reporter of every terminology failure. The defect this covers
                # shows as the checks never running, which "cannot run" names.
                self.assertIn("Checking terminology", proc.stdout)

    def test_no_floor_fires_on_a_search_that_did_not_run(self):
        # A "no document states this any more" error is a wrong diagnosis when
        # the search itself failed: `search` returns 2 and prints nothing, and
        # every floor below then announces its own cap is undocumented. Both
        # floors gate on the search having run.
        source = GUARD.read_text(encoding="utf-8")
        for guarded in ('if [ "$probe_ok" -eq 0 ]', 'if [ "$ID_SEARCH_OK" -eq 0 ]'):
            self.assertIn(guarded, source)
        floors = re.findall(r"^.*-lt 1 \]; then$", source, re.MULTILINE)
        self.assertEqual(len(floors), 2, f"unguarded floor added? {floors}")


if __name__ == "__main__":
    unittest.main()
