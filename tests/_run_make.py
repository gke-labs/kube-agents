"""Run `make` from a test, the way a developer runs it by hand.

A test module that drives `make` may itself be running inside the
`make test-python` sweep, and an inherited jobserver or MAKELEVEL would make
the nested make behave unlike the one a developer runs. Scrubbing those is one
piece of knowledge, kept here so a further variable to drop or a change of
policy is fixed once rather than in each module that happens to import it. Not
a test module itself: `test_*.py` is the discovery pattern.
"""

import os
import pathlib
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Environment variables an enclosing make leaves behind that would change how
#: a nested one behaves.
INHERITED_MAKE_VARS = ("MAKEFLAGS", "MAKELEVEL")


def run_make(args, timeout, cwd=None, extra_env=None):
    """Run `make <args>` and return the CompletedProcess.

    Defaults to the repository root, which is what the callers driving the root
    Makefile want. `cwd` covers the nested Makefiles -- k8s-operator/ is the
    only one today -- so a test driving one of those gets the same scrubbing
    rather than reimplementing it.

    `extra_env` is for the process environment, which is not the same thing as
    a `VAR=value` argument: a test that shadows a binary on PATH has to reach
    the environment, and doing that by hand means rebuilding the scrubbing
    above at the call site.
    """
    env = dict(os.environ)
    for name in INHERITED_MAKE_VARS:
        env.pop(name, None)
    env.update(extra_env or {})
    return subprocess.run(
        ["make", *args],
        cwd=cwd or REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
