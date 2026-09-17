"""`make help` still lists the lint targets.

`make help` is the documentation of the Makefile's targets: it prints every
recipe line that carries a `## description`, and nothing else in the
repository names the targets. A target whose `##` comment is dropped, or which
is renamed or deleted, therefore vanishes from the only place a contributor
would look, and no other check notices. `lint-python` is the developer entry
point for a gate the unit suite runs (`tests/test_lint_python.py`) and
`shellcheck` for the one the `validate` job in `.github/workflows/validate.yml`
runs (tests/test_shellcheck_gate_wiring.py pins that step); `make help` is
where a contributor discovers both, so this pins them to the help output.
"""

import pathlib
import re
import sys
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _run_make import run_make  # noqa: E402

#: Targets `make help` must list, each with a `## description` on its recipe
#: line in the Makefile.
REQUIRED_TARGETS = ("shellcheck", "lint-python")

#: `make help` colours the target names; strip the escapes before matching.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

#: A help line is two spaces, the target name, whitespace, then its description.
HELP_LINE = "  {target} "

MAKE_TIMEOUT_SECONDS = 60


def _make_help():
    result = run_make(["help"], timeout=MAKE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise AssertionError(
            "make help failed (%d):\n%s" % (result.returncode, result.stderr)
        )
    return ANSI_ESCAPE.sub("", result.stdout)


class MakeHelpListsLintTargetsTest(unittest.TestCase):
    def test_lint_targets_are_listed(self):
        help_text = _make_help()
        for target in REQUIRED_TARGETS:
            with self.subTest(target=target):
                self.assertTrue(
                    any(
                        line.startswith(HELP_LINE.format(target=target))
                        for line in help_text.splitlines()
                    ),
                    "`make help` no longer lists %r; its recipe line lost its "
                    "`## description` or the target was renamed:\n%s"
                    % (target, help_text),
                )


if __name__ == "__main__":
    unittest.main()
