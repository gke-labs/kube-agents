"""`make help` still lists the lint targets.

`make help` is the documentation of the Makefile's targets: it prints every
recipe line that carries a `## description`, and nothing else in the
repository names the targets. A target whose `##` comment is dropped, or which
is renamed or deleted, therefore vanishes from the only place a contributor
would look, and no other check notices. The `shellcheck` and `lint-python`
targets are the developer entry points for two gates that otherwise run only
by hand and from the unit suite, so this pins them to the help output.
"""

import os
import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Targets `make help` must list, each with a `## description` on its recipe
#: line in the Makefile.
REQUIRED_TARGETS = ("shellcheck", "lint-python")

#: `make help` colours the target names; strip the escapes before matching.
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

#: A help line is two spaces, the target name, whitespace, then its description.
HELP_LINE = "  {target} "

MAKE_TIMEOUT_SECONDS = 60


def _make_help():
    env = dict(os.environ)
    # This test may itself be running inside the `make test-python` sweep, and
    # an inherited jobserver or MAKELEVEL would make the nested make behave
    # unlike the one a developer runs by hand.
    env.pop("MAKEFLAGS", None)
    env.pop("MAKELEVEL", None)
    result = subprocess.run(
        ["make", "help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=MAKE_TIMEOUT_SECONDS,
    )
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
