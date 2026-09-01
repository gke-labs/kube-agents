"""Conformance tests for the security and permissions invariants.

One test (or one written reason for its absence) per invariant in the F10
requirements set. See README.md in this directory for the invariant -> test ->
bucket -> historical-attack table.
"""


import unittest as _unittest


def load_tests(loader, standard_tests, pattern):
    """Keep root discovery out; run.py is the entry.

    `make test-python` discovers from `tests/`, and a package under it is
    recursed into — which ran this suite a second time per pull request and
    surfaced bucket 2 as skips, the presentation the bucket split exists to
    prevent. Returning an empty suite here makes the exclusion real rather
    than prose; run.py is unaffected because it loads the test modules by
    name and never triggers package-level discovery.
    """
    del loader, standard_tests, pattern
    return _unittest.TestSuite()
