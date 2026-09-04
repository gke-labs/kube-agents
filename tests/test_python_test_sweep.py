"""A failing directory in the `make test-python` sweep still fails the build.

The sweep runs PYTHON_TEST_DIRS concurrently, so a directory's verdict can no
longer be a shell variable -- a subprocess cannot assign one the parent will
see. It travels through a per-directory file in a temp directory instead, and
that is a channel with ways to go quiet: a write that fails, a name that
collides, a parent loop that reads the wrong path. Every one of them looks the
same from outside -- `make test-python` exits 0 with a red directory in the run
-- which is the failure this repository's suite most needs not to have.

Nothing else covers it. `scripts/test_test_discovery.py` checks which
directories the sweep *reaches*; this checks what its verdict does to the exit
status. Both job counts run, because serial and concurrent are separate paths
through the same macro and only the concurrent one is new.

Most of this drives `sweep_python_test_dirs` directly through a wrapper
makefile rather than through `make test-python`, which is worth the indirection
purely for time: the target's missing-import preflight starts twenty Python
interpreters, and paying that six times would add half a minute to the suite
this sweep exists to shorten. One case does go through the real target, since
what the macro leaves in `$failed` matters only if the caller turns it into a
non-zero exit.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: A real directory with no test_*.py in it. Discovery there finds nothing and
#: succeeds, which is the "green" half of each case below at near-zero cost.
EMPTY_DIR = "docs/"
#: A directory that does not exist, so the sweep's `cd` fails before the
#: per-directory command runs at all. Cheaper than a directory holding a
#: deliberately failing test, and it exercises the same path out of the worker.
MISSING_DIR = "nosuchdir/"
#: Serial and concurrent are separate paths through sweep_python_test_dirs.
JOB_COUNTS = (1, 2)
#: Printed by the probe target below so the test can read `$failed` back.
FAILED_MARKER = "SWEEP-FAILED:"
SWEEP_TIMEOUT_SECONDS = 300


def _has_coverage():
    """Whether `make coverage` can get past `coverage run` at all.

    Asked of `python3` by subprocess rather than with an import, because that
    is the interpreter the Makefile invokes -- this test may be running under
    a different one.

    Everything above needs only make and python3, which is what lets
    agent-startup-test.yml run this file with pyyaml as its single dependency,
    deliberately, so the tests run anywhere the agent image does. The coverage
    cases below need the package too. Without this they do not fail honestly
    there: the strict case asserts a non-zero exit and gets one from the
    missing package rather than from the gate, so it passes while testing
    nothing. The `test` job installs requirements-test.txt, which is the job
    whose verdict the gate controls and where these must not skip.
    """
    return (
        subprocess.run(
            ["python3", "-m", "coverage", "--version"],
            capture_output=True,
        ).returncode
        == 0
    )

#: A target that runs the sweep over a trivial command and reports the one
#: thing the macro promises its callers: what `$failed` holds afterwards.
PROBE_MAKEFILE = f"""\
include Makefile
sweep-probe:
\t@$(call sweep_python_test_dirs,true) >/dev/null; echo "{FAILED_MARKER}[$$failed]"
"""


def _run_make(args):
    env = dict(os.environ)
    # This test may itself be running inside the sweep, and an inherited
    # jobserver or MAKELEVEL would make the nested make behave unlike the one a
    # developer runs by hand.
    env.pop("MAKEFLAGS", None)
    env.pop("MAKELEVEL", None)
    return subprocess.run(
        ["make", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=SWEEP_TIMEOUT_SECONDS,
    )


def sweep(dirs, jobs):
    """Run the macro over `dirs`, returning (completed process, `$failed`)."""
    with tempfile.NamedTemporaryFile("w", suffix=".mk", delete=False) as wrapper:
        wrapper.write(PROBE_MAKEFILE)
        wrapper_path = wrapper.name
    try:
        done = _run_make(
            [
                "-f",
                wrapper_path,
                "sweep-probe",
                f"PYTHON_TEST_DIRS={' '.join(dirs)}",
                f"PYTHON_TEST_JOBS={jobs}",
            ]
        )
    finally:
        os.unlink(wrapper_path)
    marker = [ln for ln in done.stdout.splitlines() if ln.startswith(FAILED_MARKER)]
    failed = marker[-1][len(FAILED_MARKER) :].strip("[]") if marker else None
    return done, failed


class SweepVerdictTest(unittest.TestCase):
    def test_a_failing_directory_is_named_in_failed(self):
        for jobs in JOB_COUNTS:
            with self.subTest(jobs=jobs):
                done, failed = sweep([EMPTY_DIR, MISSING_DIR], jobs)
                self.assertEqual(MISSING_DIR, failed, done.stdout + done.stderr)

    def test_a_clean_sweep_leaves_failed_empty(self):
        # The other direction, so the test above cannot pass by the macro
        # reporting everything as failed.
        for jobs in JOB_COUNTS:
            with self.subTest(jobs=jobs):
                done, failed = sweep([EMPTY_DIR], jobs)
                self.assertEqual("", failed, done.stdout + done.stderr)

    def test_every_directory_runs_even_after_one_fails(self):
        # The property the sequential loop had and the sweep had to re-earn: one
        # failure must not stop the directories after it. A `set -e` regression
        # here would hide whole suites behind a familiar-looking red run.
        done, _ = sweep([MISSING_DIR, EMPTY_DIR], max(JOB_COUNTS))
        self.assertIn(f"==> {EMPTY_DIR}", done.stdout)
        self.assertIn(f"==> {MISSING_DIR}", done.stdout)


class TestPythonExitStatusTest(unittest.TestCase):
    def test_the_target_exits_non_zero_when_a_directory_fails(self):
        # The one end-to-end case: `$failed` is only worth setting if the caller
        # acts on it, and a sweep that reports correctly into a target that
        # swallows the result is the same green-on-red as no sweep at all.
        done = _run_make(
            [
                "test-python",
                f"PYTHON_TEST_DIRS={EMPTY_DIR} {MISSING_DIR}",
                f"PYTHON_TEST_JOBS={max(JOB_COUNTS)}",
            ]
        )
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn(MISSING_DIR, done.stdout.split("Failing test directories:")[-1])


@unittest.skipUnless(_has_coverage(), "the coverage package is not installed")
class CoverageStrictTest(unittest.TestCase):
    """`make coverage COVERAGE_STRICT=1` turns the same `$failed` into an exit.

    The argument is TestPythonExitStatusTest's, for the other caller of the
    sweep. It matters more here: `coverage` is the target CI's required job
    runs, and the target tolerates a failing directory by default -- it is the
    meter, and one red directory must not hide the number for the rest. Strict
    mode is the only thing making a red suite a red check, so an unnoticed
    regression in it reports success on failing tests.

    Two cases rather than four, because each one that reaches the end of the
    target costs about nine seconds -- `coverage xml` and `coverage report`
    walk the source tree whether or not the sweep produced any data, and
    tests/ is already one of the slower directories in the sweep these run
    inside. The pair below pins the gate to the flag in both directions. The
    third case, strict mode passing on a green sweep, is what every green run
    of the CI job already demonstrates, so buying it again here is nine
    seconds for nothing.
    """

    #: Emptying it skips the target's missing-import preflight, which starts one
    #: interpreter per entry -- pure cost here, since the sweep runs no tests.
    NO_PREFLIGHT = "PYTHON_TEST_IMPORTS="

    def _coverage(self, strict, dirs=(EMPTY_DIR, MISSING_DIR)):
        # Every output path is redirected into a temp directory. tests/ is a
        # PYTHON_TEST_DIR, so under CI this runs *inside* `make coverage`, and
        # with the defaults the nested run's `rm -rf` would delete the outer
        # run's data mid-sweep.
        #
        # Under the repository root, and COVERAGE_DIR passed relative to it,
        # because the target composes `$(CURDIR)/$(COVERAGE_DIR)`: an absolute
        # path there is concatenated rather than used, so /tmp/x becomes
        # <repo>/tmp/x and the data lands in the working tree. It does that
        # quietly -- the run still passes.
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as out:
            relative = pathlib.Path(out).relative_to(REPO_ROOT)
            return _run_make(
                [
                    "coverage",
                    f"PYTHON_TEST_DIRS={' '.join(dirs)}",
                    f"PYTHON_TEST_JOBS={max(JOB_COUNTS)}",
                    f"COVERAGE_STRICT={strict}",
                    "COVERAGE_SKIP_GO=1",
                    self.NO_PREFLIGHT,
                    f"COVERAGE_DIR={relative}/data",
                    f"COVERAGE_XML={out}/coverage.xml",
                    f"COVERAGE_GO_XML={out}/coverage-go.xml",
                ]
            )

    def test_strict_fails_when_a_directory_fails(self):
        done = self._coverage(1)
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn(MISSING_DIR, done.stdout.split("FAIL (COVERAGE_STRICT=1)")[-1])

    def test_the_default_still_reports_the_number_on_a_red_directory(self):
        # The other direction. Strict mode is opt-in precisely so a local run
        # against a tree with known-red directories still prints a total.
        done = self._coverage(0)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("TOTAL", done.stdout)

    def test_a_value_that_is_neither_0_nor_1_is_refused(self):
        # Not guessed. Every truthy-looking spelling reads as "not 1" to the
        # gate, which turns it off silently -- the one failure the flag exists
        # to prevent. Refusing costs a typo'd run; guessing costs the gate.
        done = self._coverage("true")
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("COVERAGE_STRICT must be 0 or 1", done.stdout)
        # And before the sweep, not after it: validated at the top of the
        # target so a typo does not cost the whole suite first.
        self.assertNotIn(f"==> {EMPTY_DIR}", done.stdout)


if __name__ == "__main__":
    unittest.main()
