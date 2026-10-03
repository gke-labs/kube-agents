"""A failing file in the `make test-python` sweep still fails the build.

The sweep runs PYTHON_TEST_FILES concurrently, so a file's verdict can no
longer be a shell variable -- a subprocess cannot assign one the parent will
see. It travels through a per-file file in a temp directory instead, and that
is a channel with ways to go quiet: a write that fails, a name that collides,
a parent loop that reads the wrong path. Every one of them looks the same from
outside -- `make test-python` exits 0 with a red file in the run -- which is
the failure this repository's suite most needs not to have.

Nothing else covers it. `scripts/test_test_discovery.py` checks which
directories the sweep *reaches*; this checks what its verdict does to the exit
status. Both job counts run, because serial and concurrent are separate paths
through the same macro and only the concurrent one is new.

Most of this drives `sweep_python_test_files` directly through a wrapper
makefile rather than through `make test-python`, which is worth the indirection
purely for time: the target's missing-import preflight starts twenty Python
interpreters, and paying that six times would add half a minute to the suite
this sweep exists to shorten. One case does go through the real target, since
what the macro leaves in `$failed` matters only if the caller turns it into a
non-zero exit.
"""

import contextlib
import os
import pathlib
import posixpath
import re
import subprocess
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _run_make import run_make  # noqa: E402

REPO_ROOT = _HERE.parent

#: A file in a directory that does not exist, so the worker's regular-file
#: check fails before the per-file command runs at all. Cheaper than a file
#: holding a deliberately failing test, and it exercises the same path out of
#: the worker.
MISSING_FILE = "nosuchdir/test_missing.py"
#: A name nothing writes under the fixture directory: a path whose directory
#: exists and whose file does not, which is the shape the `cd` would not catch.
ABSENT_FILE = "test_absent.py"
#: The old way to narrow a run, which the per-file sweep must refuse rather
#: than accept and ignore.
DIRS_OVERRIDE = "PYTHON_TEST_DIRS=tests/"
#: The refusal's own example of narrowing a run, as make prints it: the
#: variable and a double-quoted shell expression.
EXAMPLE_PATTERN = re.compile(r'PYTHON_TEST_FILES="[^"]*"')
#: Where the example's glob looks, and two files for it to match. The shape
#: that broke needs the glob to expand to more than one path to show itself.
EXAMPLE_DIR = "tests"
EXAMPLE_FILES = ("test_a.py", "test_b.py")
#: Expands a shell assignment and prints the value it assigned.
EXPAND_ASSIGNMENT = 'eval "$1" && printf %s "$PYTHON_TEST_FILES"'
#: The two fixture files `fixture_files` writes: one test that passes and one
#: that fails, side by side in one directory, so a case can show that the
#: failing one does not take its sibling down with it.
GREEN_FILE = "test_green.py"
RED_FILE = "test_red.py"
GREEN_SOURCE = """\
import unittest


class GreenTest(unittest.TestCase):
    def test_passes(self):
        pass
"""
RED_SOURCE = """\
import unittest


class RedTest(unittest.TestCase):
    def test_fails(self):
        self.fail("planted")
"""
#: Serial and concurrent are separate paths through sweep_python_test_files.
JOB_COUNTS = (1, 2)
#: Printed by the probe target below so the test can read `$failed` back.
FAILED_MARKER = "SWEEP-FAILED:"
#: Printed by the lists target below, one list per line.
FILES_MARKER = "SWEEP-FILES:"
DIRS_MARKER = "SWEEP-DIRS:"
SWEEP_TIMEOUT_SECONDS = 300


def _has_coverage():
    """Whether `make coverage` can get past `coverage run` at all.

    Asked of `python3` by subprocess rather than with an import, because that
    is the interpreter the Makefile invokes -- this test may be running under
    a different one.

    Everything above needs only make and python3, so those cases run anywhere
    the agent image does, with nothing installed. The coverage cases below need
    the package too. Without this they do not fail honestly
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


@contextlib.contextmanager
def fixture_files():
    """Write the green and red fixtures and yield their absolute paths.

    Outside the repository on purpose. `scripts/test_test_discovery.py` walks
    the tree for test_*.py files and now runs concurrently with this module,
    so a fixture under the repository root would be, for as long as the case
    lasts, a test directory no glob reaches -- and that test would be right to
    fail on it. The sweep takes any path it can `cd` to the directory of, so
    an absolute one outside the tree is as good as a relative one inside it.
    """
    with tempfile.TemporaryDirectory() as out:
        green = os.path.join(out, GREEN_FILE)
        red = os.path.join(out, RED_FILE)
        pathlib.Path(green).write_text(GREEN_SOURCE)
        pathlib.Path(red).write_text(RED_SOURCE)
        yield green, red


#: A target that runs the sweep over a trivial command and reports the one
#: thing the macro promises its callers: what `$failed` holds afterwards. The
#: command is `true`, so the only way a file fails is the worker's `cd`.
PROBE_MAKEFILE = f"""\
include Makefile
sweep-probe:
\t@$(call sweep_python_test_files,true) >/dev/null; echo "{FAILED_MARKER}[$$failed]"
"""

#: The same probe over the real discovery command, with the captured blocks
#: left on stdout, for the cases about what one file's failure does to another.
DISCOVER_MAKEFILE = f"""\
include Makefile
sweep-probe:
\t@$(call sweep_python_test_files,python3 -m unittest discover); echo "{FAILED_MARKER}[$$failed]"
"""

#: Prints both lists as make expands them, for the derivation case.
LISTS_MAKEFILE = f"""\
include Makefile
sweep-lists:
\t@echo "{FILES_MARKER}$(PYTHON_TEST_FILES)"; echo "{DIRS_MARKER}$(PYTHON_TEST_DIRS)"
"""


def _run_make(args, cwd=None):
    return run_make(args, timeout=SWEEP_TIMEOUT_SECONDS, cwd=cwd)


def _with_wrapper(makefile, target, args, cwd=None):
    """Run `target` from a wrapper makefile that includes the repository's.

    From another `cwd`, both that include and the Makefile's own `include
    tags.env` are resolved relative to the working directory, so `-I` points
    them at the repository.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".mk", delete=False) as wrapper:
        wrapper.write(makefile)
        wrapper_path = wrapper.name
    try:
        return _run_make(["-I", str(REPO_ROOT), "-f", wrapper_path, target, *args], cwd=cwd)
    finally:
        os.unlink(wrapper_path)


def _failed(stdout):
    """What the probe's marker line says `$failed` held; None if it never printed."""
    marker = [ln for ln in stdout.splitlines() if ln.startswith(FAILED_MARKER)]
    return marker[-1][len(FAILED_MARKER) :].strip("[]") if marker else None


def sweep(files, jobs, makefile=PROBE_MAKEFILE):
    """Run the macro over `files`, returning (completed process, `$failed`)."""
    done = _with_wrapper(
        makefile,
        "sweep-probe",
        [f"PYTHON_TEST_FILES={' '.join(files)}", f"PYTHON_TEST_JOBS={jobs}"],
    )
    return done, _failed(done.stdout)


def _refuse_dirs_override(green):
    """`make test-python` with the old override typed in, over one green file.

    One green file so that, were the guard missing, the run would end in a
    second on a passing sweep rather than after a run of the whole tree.
    """
    return _run_make(
        [
            "test-python",
            DIRS_OVERRIDE,
            f"PYTHON_TEST_FILES={green}",
            "PYTHON_TEST_IMPORTS=",
        ]
    )


def _block(stdout, path):
    """The captured output printed under `==> path`, up to the next header.

    The probe's own marker line is cut off too. It follows the last block, and
    `FAILED` is a substring of it, so a block that ran to the end of stdout would
    satisfy an assertion about a red shard whatever the shard printed.
    """
    after = stdout.split(f"==> {path}\n", 1)[1]
    block = after.split("\n==> ", 1)[0]
    return block.split(f"\n{FAILED_MARKER}", 1)[0]


class SweepVerdictTest(unittest.TestCase):
    def test_a_failing_file_is_named_in_failed(self):
        with fixture_files() as (green, _):
            for jobs in JOB_COUNTS:
                with self.subTest(jobs=jobs):
                    done, failed = sweep([green, MISSING_FILE], jobs)
                    self.assertEqual(MISSING_FILE, failed, done.stdout + done.stderr)

    def test_a_clean_sweep_leaves_failed_empty(self):
        # The other direction, so the test above cannot pass by the macro
        # reporting everything as failed.
        with fixture_files() as (green, _):
            for jobs in JOB_COUNTS:
                with self.subTest(jobs=jobs):
                    done, failed = sweep([green], jobs)
                    self.assertEqual("", failed, done.stdout + done.stderr)

    def test_every_file_runs_even_after_one_fails(self):
        # The property the sequential loop had and the sweep had to re-earn: one
        # failure must not stop the files after it. A `set -e` regression
        # here would hide whole suites behind a familiar-looking red run.
        with fixture_files() as (green, _):
            done, _ = sweep([MISSING_FILE, green], max(JOB_COUNTS))
            self.assertIn(f"==> {green}", done.stdout)
            self.assertIn(f"==> {MISSING_FILE}", done.stdout)

    def test_a_file_beside_a_failing_one_still_gets_its_own_block(self):
        # Sharding by file rather than by directory is the whole point of the
        # macro, and this is the property it has to deliver: two files in one
        # directory are two shards. The failing one is named alone in
        # `$failed`, and its sibling's block shows the one test it collected,
        # not the failing file's -- so `-p <name>` reached the discovery
        # command and narrowed it to the file.
        with fixture_files() as (green, red):
            done, failed = sweep([green, red], max(JOB_COUNTS), DISCOVER_MAKEFILE)
            self.assertEqual(red, failed, done.stdout + done.stderr)
            green_block = _block(done.stdout, green)
            self.assertIn("Ran 1 test", green_block)
            self.assertIn("OK", green_block)
            red_block = _block(done.stdout, red)
            self.assertIn("Ran 1 test", red_block)
            self.assertIn("FAILED (failures=1)", red_block)

    def test_a_path_that_is_not_a_file_is_named_in_failed(self):
        # A directory, or a file that is not there, must not read green. Rooted
        # at its dirname with its basename as the pattern, either collects
        # nothing, and Python 3.11 exits 0 on an empty collection -- so the
        # worker refuses the path before discovery runs and says why in the
        # block. The directory is the old `PYTHON_TEST_DIRS=tests/` habit typed
        # onto the new variable; the absent file is the shape a failing `cd`
        # would never catch.
        with fixture_files() as (green, _):
            directory = os.path.dirname(green)
            for path in (directory, os.path.join(directory, ABSENT_FILE)):
                with self.subTest(path=path):
                    done, failed = sweep([green, path], max(JOB_COUNTS), DISCOVER_MAKEFILE)
                    self.assertEqual(path, failed, done.stdout + done.stderr)
                    self.assertIn("not a file", _block(done.stdout, path))


class DerivedDirectoriesTest(unittest.TestCase):
    def test_python_test_dirs_is_the_parents_of_python_test_files(self):
        # PYTHON_TEST_DIRS is derived from PYTHON_TEST_FILES now, and
        # scripts/test_test_discovery.py still reads the derived list to
        # decide whether every test directory is reached. This pins the
        # derivation, so that test keeps meaning what it says.
        done = _with_wrapper(LISTS_MAKEFILE, "sweep-lists", [])
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        lines = dict(
            ln.split(":", 1) for ln in done.stdout.splitlines() if ln.startswith("SWEEP-")
        )
        files = lines[FILES_MARKER.rstrip(":")].split()
        dirs = {d.rstrip("/") for d in lines[DIRS_MARKER.rstrip(":")].split()}
        self.assertTrue(files, "PYTHON_TEST_FILES expanded to nothing")
        self.assertEqual({posixpath.dirname(f) for f in files}, dirs)

    def test_a_command_line_python_test_dirs_is_refused(self):
        # Derived means not an input. A command-line value would still beat the
        # derivation, so the discovery test's wrapper would print it as the
        # whole list while the sweep, which reads only PYTHON_TEST_FILES, ran
        # every file: the caller asked for one directory and paid for the
        # tree. Make refuses it at parse time, before any target runs, naming
        # the variable that does narrow a run.
        with fixture_files() as (green, _):
            done = _refuse_dirs_override(green)
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("PYTHON_TEST_FILES", done.stderr)
        self.assertNotIn("==> ", done.stdout)

    def test_the_refusals_example_runs_when_pasted_into_make(self):
        # The refusal is read by someone about to type the next command, and
        # its example has to survive being pasted into it. `$(ls ...)` did
        # not: with two or more matches and a pipe for stdout, ls prints one
        # path per line, the variable arrives holding newlines, and make ends a
        # recipe command at each one -- the first line of test-python became an
        # unterminated `if [ -z "tests/test_a.py` and the target stopped on a
        # shell syntax error that named neither variable. So the example is
        # read back out of the refusal, expanded by a shell in a directory
        # where its glob matches two files, and handed to the macro as the one
        # argument the pasted command line would hand it.
        with fixture_files() as (green, _):
            example = EXAMPLE_PATTERN.search(_refuse_dirs_override(green).stderr)
        self.assertIsNotNone(example, "the refusal no longer shows an example")
        with tempfile.TemporaryDirectory() as out:
            os.mkdir(os.path.join(out, EXAMPLE_DIR))
            for name in EXAMPLE_FILES:
                pathlib.Path(out, EXAMPLE_DIR, name).write_text(GREEN_SOURCE)
            value = subprocess.run(
                ["sh", "-c", EXPAND_ASSIGNMENT, "_", example.group(0)],
                cwd=out,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            done = _with_wrapper(
                PROBE_MAKEFILE,
                "sweep-probe",
                [f"PYTHON_TEST_FILES={value}", f"PYTHON_TEST_JOBS={max(JOB_COUNTS)}"],
                cwd=out,
            )
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual("", _failed(done.stdout), done.stdout)
        for name in EXAMPLE_FILES:
            self.assertIn(f"==> {posixpath.join(EXAMPLE_DIR, name)}", done.stdout)


class TestPythonExitStatusTest(unittest.TestCase):
    def test_the_target_exits_non_zero_when_a_file_fails(self):
        # The one end-to-end case: `$failed` is only worth setting if the caller
        # acts on it, and a sweep that reports correctly into a target that
        # swallows the result is the same green-on-red as no sweep at all.
        with fixture_files() as (green, _):
            done = _run_make(
                [
                    "test-python",
                    f"PYTHON_TEST_FILES={green} {MISSING_FILE}",
                    f"PYTHON_TEST_JOBS={max(JOB_COUNTS)}",
                ]
            )
            self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
            named = done.stdout.split("Failing test files:")[-1]
            self.assertIn(MISSING_FILE, named)
            # And the real discovery command, pattern appended, ran the green
            # file rather than failing it: a `-p` that did not reach the command
            # would collect every test_*.py around the fixture or none.
            self.assertNotIn(green, named)


@unittest.skipUnless(_has_coverage(), "the coverage package is not installed")
class CoverageStrictTest(unittest.TestCase):
    """`make coverage COVERAGE_STRICT=1` turns the same `$failed` into an exit.

    The argument is TestPythonExitStatusTest's, for the other caller of the
    sweep. It matters more here: `coverage` is the target CI's required job
    runs, and the target tolerates a failing file by default -- it is the
    meter, and one red file must not hide the number for the rest. Strict
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

    def _coverage(self, strict):
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
        with tempfile.TemporaryDirectory(dir=REPO_ROOT) as out, fixture_files() as (
            green,
            _,
        ):
            relative = pathlib.Path(out).relative_to(REPO_ROOT)
            return green, _run_make(
                [
                    "coverage",
                    f"PYTHON_TEST_FILES={green} {MISSING_FILE}",
                    f"PYTHON_TEST_JOBS={max(JOB_COUNTS)}",
                    f"COVERAGE_STRICT={strict}",
                    "COVERAGE_SKIP_GO=1",
                    self.NO_PREFLIGHT,
                    f"COVERAGE_DIR={relative}/data",
                    f"COVERAGE_XML={out}/coverage.xml",
                    f"COVERAGE_GO_XML={out}/coverage-go.xml",
                ]
            )

    def test_strict_fails_when_a_file_fails(self):
        _, done = self._coverage(1)
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn(MISSING_FILE, done.stdout.split("FAIL (COVERAGE_STRICT=1)")[-1])

    def test_the_default_still_reports_the_number_on_a_red_file(self):
        # The other direction. Strict mode is opt-in precisely so a local run
        # against a tree with known-red files still prints a total.
        _, done = self._coverage(0)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("TOTAL", done.stdout)

    def test_a_value_that_is_neither_0_nor_1_is_refused(self):
        # Not guessed. Every truthy-looking spelling reads as "not 1" to the
        # gate, which turns it off silently -- the one failure the flag exists
        # to prevent. Refusing costs a typo'd run; guessing costs the gate.
        green, done = self._coverage("true")
        self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("COVERAGE_STRICT must be 0 or 1", done.stdout)
        # And before the sweep, not after it: validated at the top of the
        # target so a typo does not cost the whole suite first.
        self.assertNotIn(f"==> {green}", done.stdout)


if __name__ == "__main__":
    unittest.main()
