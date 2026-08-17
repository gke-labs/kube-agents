#!/usr/bin/env python3
"""Run the conformance suite standalone.

`python3 -m unittest discover -s tests` finds this suite too, and that is how
CI runs it. This exists so the suite can be run on its own without anyone
having to remember that `-t tests` is required for the package-relative imports
to resolve — the sort of invocation detail that ends with someone running
`discover -s tests/conformance`, collecting zero tests, and reading the `OK`.

    python3 tests/conformance/run.py            # bucket 1
    python3 tests/conformance/run.py --bucket2  # include the cluster scenarios
    python3 tests/conformance/run.py -q         # one line per test class

Exit code is 0 when every bucket-1 assertion holds and every recorded known
violation still fails in the way it is recorded as failing. An *unexpected
success* is a non-zero exit and it means a gap has closed: go delete the
decorator.
"""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path

CONFORMANCE_DIR = Path(__file__).resolve().parent
TESTS_DIR = CONFORMANCE_DIR.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bucket2",
        action="store_true",
        help="also run the cluster scenarios (needs KUBE_AGENTS_CONFORMANCE_CLUSTER)",
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-k", "--pattern", default="test_*.py")
    arguments = parser.parse_args()

    if arguments.bucket2 and not os.environ.get("KUBE_AGENTS_CONFORMANCE_CLUSTER"):
        print(
            "--bucket2 needs KUBE_AGENTS_CONFORMANCE_CLUSTER set to a kubectl "
            "context. Refusing rather than reporting a suite of skips as a pass.",
            file=sys.stderr,
        )
        return 2

    sys.path.insert(0, str(TESTS_DIR))
    loader = unittest.TestLoader()
    suite = loader.discover(
        start_dir=str(CONFORMANCE_DIR),
        pattern=arguments.pattern,
        top_level_dir=str(TESTS_DIR),
    )
    if loader.errors:
        # discover() reports an unimportable module as a synthetic failing test.
        # Saying so here as well, because "1 test, 1 error" from a suite of a
        # hundred is easy to read as a single broken assertion.
        print(
            f"{len(loader.errors)} test module(s) failed to import; the suite "
            f"below is incomplete.",
            file=sys.stderr,
        )

    if not arguments.bucket2:
        suite = _without_bucket2(suite)

    collected = suite.countTestCases()
    if collected == 0:
        print("no tests were collected", file=sys.stderr)
        return 2

    result = unittest.TextTestRunner(verbosity=1 if arguments.quiet else 2).run(suite)
    return 0 if result.wasSuccessful() else 1


def _without_bucket2(suite: unittest.TestSuite) -> unittest.TestSuite:
    """Drop the cluster scenarios rather than letting them report as skips.

    A skip and a bucket-1 pass look the same in a summary line, and the whole
    point of the bucket split is that the two are different claims.
    """
    keep = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            child = _without_bucket2(test)
            if child.countTestCases():
                keep.addTest(child)
        elif "bucket2" not in type(test).__module__:
            keep.addTest(test)
    return keep


if __name__ == "__main__":
    raise SystemExit(main())
