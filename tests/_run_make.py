"""Run the root Makefile from a test, the way a developer runs it by hand.

The test modules that drive `make` (`test_python_test_sweep.py`,
`test_make_help_targets.py`) may themselves be running inside the
`make test-python` sweep, and an inherited jobserver or MAKELEVEL would make
the nested make behave unlike the one a developer runs. Scrubbing those is one
piece of knowledge, kept here so a further variable to drop or a change of
policy cannot be fixed in one module and missed in the other. Not a test
module itself: `test_*.py` is the discovery pattern.
"""

import os
import pathlib
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Environment variables an enclosing make leaves behind that would change how
#: a nested one behaves.
INHERITED_MAKE_VARS = ("MAKEFLAGS", "MAKELEVEL")


def run_make(args, timeout):
    """Run `make <args>` at the repository root and return the CompletedProcess."""
    env = dict(os.environ)
    for name in INHERITED_MAKE_VARS:
        env.pop(name, None)
    return subprocess.run(
        ["make", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
