#!/usr/bin/env python3
"""Run the conformance suite. This runner is the suite's only entry.

Root discovery (`make test-python`'s `discover -s tests`) deliberately
collects nothing from this package: the package __init__ defines a
`load_tests` that returns an empty suite, because the alternative was the
suite running twice per pull request with bucket 2 surfacing as skips in the
run that cannot honour the bucket split. This file therefore loads the test
modules by name rather than by discovery, so the guard cannot hide the suite
from its own runner.

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
import fnmatch
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
    suite = unittest.TestSuite()
    module_files = sorted(CONFORMANCE_DIR.glob("test_*.py")) + sorted(
        (CONFORMANCE_DIR / "bucket2").glob("test_*.py")
    )
    import_failures = 0
    for module_file in module_files:
        if not fnmatch.fnmatch(module_file.name, arguments.pattern):
            continue
        name = ".".join(module_file.relative_to(TESTS_DIR).with_suffix("").parts)
        try:
            suite.addTests(loader.loadTestsFromName(name))
        except Exception as error:  # noqa: BLE001 - report and keep loading
            import_failures += 1
            print(f"failed to import {name}: {error}", file=sys.stderr)
    if import_failures:
        # An unimportable module would otherwise vanish from the run entirely.
        # Saying so here, because a green suite that quietly lost a module is
        # the failure mode this runner exists to prevent.
        print(
            f"{import_failures} test module(s) failed to import; the suite "
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
    if import_failures:
        return 1
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
