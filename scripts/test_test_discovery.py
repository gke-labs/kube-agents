"""Every test_*.py in the tree either runs in CI or is excluded here, by name.

`make test-python` discovers tests from PYTHON_TEST_DIRS, a fixed list of
wildcards. A test directory the wildcards miss does not fail anything -- it
just never runs, and the suite reports green around it. That is how eight test
files (the memory provider's six and bench's two) sat unexecuted on every
pull request for months: nothing owned the difference between "excluded on
purpose" and "missed by a glob".

This test owns that difference. It walks the tree for test_*.py files,
subtracts EXCLUDED, and asserts every surviving directory is discovered. From
here on, skipping a directory means adding a reviewed line to EXCLUDED with a
reason -- it cannot happen by accident of a glob again.

It owns the opposite direction too, because that one is quieter: an EXCLUDED
entry naming a directory the globs already reach states a policy that is not in
force, and the dict stops being a description of what CI does. Promoting a tier
out of exclusion is two edits in two files, and the checks below fail unless
both land.

PYTHON_TEST_DIRS is read by invoking make itself on a wrapper makefile, not by
parsing the Makefile's text: the value that matters is the one make expands,
and a regex re-implementation would drift from it.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Directory prefixes (repo-relative, POSIX) whose test files deliberately do
# not run under `make test-python`. Every entry carries its reason; an entry
# without one should not survive review.
EXCLUDED = {
    # Has its own Makefile target (`make -C k8s-operator test-python`) and its
    # own CI workflow; the root suite does not reach into the operator.
    "k8s-operator": "own suite, k8s-operator-test.yml",
    # pytest-native (fixtures, parametrize); unittest discovery collects two
    # of its tests and errors on both. Runs under `make test-bench`.
    "bench/tests": "pytest-native, runs under make test-bench",
    # Live GKE cluster E2E test suite; pytest-native, requires live cluster, Workload Identity,
    # and KMS. Runs under `make test-e2e` in e2e-nightly-matrix.yml and e2e-manual-runner.yml.
    "tests/e2e": "live cluster E2E suite, runs under make test-e2e",
    # Live black-box CUJ journeys against a provisioned kube-agents install;
    # they open an admin portal and talk to a deployed agent, so they are
    # deliberately manual: `uv run --project bench pytest -s bench/cuj`.
    "bench/cuj": "live manual suite, needs a provisioned install",
    # tests/integration left this list when the seam tier came off probation
    # and joined PYTHON_TEST_DIRS; the contradiction check below now keeps it
    # out mechanically, so no comment has to.
}

# Directory names that are never test homes, at any depth. .terraform holds
# provider and module downloads (an initialized module can carry its own
# upstream test files), so a worktree where tofu init ever ran would
# otherwise red this guard on vendored tests.
IGNORED_NAMES = {".venv", "node_modules", "__pycache__", ".git", ".coverage-data", ".terraform", ".claude"}


def discovered_dirs():
    """The directories `make test-python` discovers, as make expands them."""
    with tempfile.NamedTemporaryFile("w", suffix=".mk", delete=False) as wrapper:
        wrapper.write("include Makefile\nprint-test-dirs:\n\t@echo $(PYTHON_TEST_DIRS)\n")
        wrapper_path = wrapper.name
    try:
        out = subprocess.run(
            ["make", "-f", wrapper_path, "print-test-dirs"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    finally:
        os.unlink(wrapper_path)
    return {d.rstrip("/") for d in out.split()}


def test_file_dirs():
    """Every directory holding a test_*.py, minus the ignored names."""
    dirs = set()
    for path in REPO_ROOT.rglob("test_*.py"):
        rel = path.relative_to(REPO_ROOT)
        if any(part in IGNORED_NAMES for part in rel.parts):
            continue
        dirs.add(rel.parent.as_posix())
    return dirs


def is_excluded(rel_dir):
    return any(rel_dir == prefix or rel_dir.startswith(prefix + "/") for prefix in EXCLUDED)


class TestEveryTestFileRuns(unittest.TestCase):
    def test_every_test_directory_is_discovered_or_excluded_by_name(self):
        discovered = discovered_dirs()
        orphans = sorted(
            d for d in test_file_dirs() if not is_excluded(d) and d not in discovered
        )
        self.assertEqual(
            orphans,
            [],
            "\n\nThese directories hold test_*.py files that never run in CI:\n  "
            + "\n  ".join(orphans)
            + "\n\nEither add a matching wildcard to PYTHON_TEST_DIRS in the "
            "Makefile, or add the directory to EXCLUDED in this file with the "
            "reason it must not run there.",
        )

    def test_the_exclusion_list_does_not_rot(self):
        # An exclusion whose directory no longer holds any test file is stale
        # noise, and stale entries are how a list stops being trusted.
        all_dirs = test_file_dirs()
        stale = sorted(
            prefix
            for prefix in EXCLUDED
            if not any(d == prefix or d.startswith(prefix + "/") for d in all_dirs)
        )
        self.assertEqual(
            stale,
            [],
            "\n\nThese EXCLUDED entries match no test_*.py directory any more; "
            "delete them:\n  " + "\n  ".join(stale),
        )

    def test_no_exclusion_names_a_directory_that_already_runs(self):
        # The orphan check above only catches one direction: a directory that
        # runs nowhere. The other direction is quieter and just as bad -- a
        # directory that PYTHON_TEST_DIRS picks up while EXCLUDED still claims
        # it is held back, so the reason string in this file describes a policy
        # that is not in force. Promoting tests/integration out of probation is
        # exactly that edit, and with only the check above, adding the Makefile
        # glob and forgetting to delete the entry here passed clean.
        discovered = discovered_dirs()
        contradicted = sorted(
            prefix
            for prefix in EXCLUDED
            if any(d == prefix or d.startswith(prefix + "/") for d in discovered)
        )
        self.assertEqual(
            contradicted,
            [],
            "\n\nThese EXCLUDED entries name directories PYTHON_TEST_DIRS "
            "already discovers, so the stated reason is not in force:\n  "
            + "\n  ".join(contradicted)
            + "\n\nEither delete the entry, or drop the Makefile wildcard that "
            "reaches the directory -- the two must tell one story.",
        )

    def test_the_wrapper_reads_a_nonempty_list(self):
        # If PYTHON_TEST_DIRS ever expands to nothing the first test would
        # vacuously report every directory as an orphan; fail with the real
        # story instead.
        self.assertTrue(
            discovered_dirs(),
            "PYTHON_TEST_DIRS expanded to nothing -- the Makefile globs are stale.",
        )


if __name__ == "__main__":
    unittest.main()
