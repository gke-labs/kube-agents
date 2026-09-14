"""ruff's error rules pass over every Python file in the repository.

No Python linter ran here before this test: no `pyproject.toml`, `setup.cfg`,
`.flake8` or `ruff.toml` at the root, so an undefined name in a module the unit
suite never imports (the one #1413 fixed by hand in
`tests/test_check_image_inventory_go_directive.py`) merges clean and fails
whenever that path is finally taken. ruff's F821 reports it statically, and
the rest of the error-only set -- syntax errors, comparisons that can never be
true, misplaced control flow -- is the same kind of defect: never a style
opinion, always a program that cannot do what it says.

Running the command from this suite rather than from a workflow is what makes
it gate: `make test-python` and `make coverage` reach `tests/test_*.py` on every
pull request already, so no workflow edit is needed and none was made.
`make lint-python` runs the identical command as the developer entry point,
and RUFF_SELECT in the Makefile is the same rule set; the Makefile test below
pins the two together.

ruff comes from requirements-test.txt (`make test-python-deps`). Where it is
not installed the test skips on a laptop, and skips on a CI job that never
installs that file at all -- agent-startup-test.yml discovers this directory
with PyYAML as its only dependency, on purpose, so the tests run anywhere the
agent image does. It fails on a CI job that did install requirements-test.txt
and still has no ruff, which is a runner that lost the dependency and must not
report the gate green. `CI` is the variable GitHub Actions sets on every job;
whether requirements-test.txt was installed is read from `coverage`, the
package `make coverage` (the job the gate exists for) runs the suite under,
which that file provides and no runner ships on its own.
"""

import importlib.util
import os
import pathlib
import re
import subprocess
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: ruff's error-only rule families. E9: syntax errors and I/O errors while
#: reading a file. F63: comparisons and asserts that are always wrong (`is`
#: with a literal, `assert` on a tuple). F7: `break`/`continue`/`return`/
#: `yield` outside the construct they belong to. F82: undefined names.
RUFF_SELECT = "E9,F63,F7,F82"

#: The command, exactly as `make lint-python` runs it. `--isolated` ignores any
#: configuration file ruff would otherwise find above or beside the tree, so
#: the verdict is the same on every machine.
RUFF_COMMAND = (
    sys.executable,
    "-m",
    "ruff",
    "check",
    "--isolated",
    "--select",
    RUFF_SELECT,
    ".",
)

#: The distribution ships a Python package of this name; `find_spec` on it is
#: the cheapest way to ask whether `python3 -m ruff` can start at all.
RUFF_MODULE = "ruff"

#: Set by GitHub Actions on every job. Where it is set and the runner did
#: install requirements-test.txt, a missing ruff is a broken runner and the
#: test fails; anywhere else, the test skips.
CI_ENV_VAR = "CI"

#: A package requirements-test.txt provides and nothing else installs: where it
#: imports, that file was installed, so ruff should have been too.
REQUIREMENTS_SENTINEL_MODULE = "coverage"

#: The Makefile assignment the developer target reads its rule set from.
MAKEFILE_RUFF_SELECT = re.compile(r"^RUFF_SELECT\s*:?=\s*(\S+)\s*$", re.MULTILINE)

RUFF_TIMEOUT_SECONDS = 300


class RuffErrorRulesTest(unittest.TestCase):
    def setUp(self):
        if importlib.util.find_spec(RUFF_MODULE) is not None:
            return
        message = (
            "ruff is not installed for %s; install it with `make test-python-deps`"
            % sys.executable
        )
        if os.environ.get(CI_ENV_VAR) and (
            importlib.util.find_spec(REQUIREMENTS_SENTINEL_MODULE) is not None
        ):
            self.fail(
                message
                + " (CI is set and requirements-test.txt was installed, so a"
                " missing linter is a failure)"
            )
        self.skipTest(message)

    def test_error_rules_pass_over_the_repository(self):
        result = subprocess.run(
            RUFF_COMMAND,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=RUFF_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            result.returncode,
            0,
            "ruff --select %s found errors (exit %d). Fix them or run "
            "`make lint-python` to see them again:\n%s%s"
            % (RUFF_SELECT, result.returncode, result.stdout, result.stderr),
        )


class MakefileRunsTheSameRulesTest(unittest.TestCase):
    """`make lint-python` and this test cannot drift apart silently."""

    def test_makefile_rule_set_matches(self):
        makefile = (REPO_ROOT / "Makefile").read_text()
        match = MAKEFILE_RUFF_SELECT.search(makefile)
        self.assertIsNotNone(match, "Makefile no longer declares RUFF_SELECT")
        self.assertEqual(
            match.group(1),
            RUFF_SELECT,
            "Makefile RUFF_SELECT differs from the set this test enforces",
        )


if __name__ == "__main__":
    unittest.main()
