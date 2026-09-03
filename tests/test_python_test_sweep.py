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


if __name__ == "__main__":
    unittest.main()
