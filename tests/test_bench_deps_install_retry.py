"""`make test-bench-deps` retries the pip install a bounded number of times.

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

The bench harness pins devops-bench to a git SHA, so a fresh runner clones it
from github.com inside `pip install -e bench/`. pip retries its HTTP downloads
but not that clone, and one GitHub-side 504 on it failed a pull request's
bench job outright while a re-run of the same tree passed. The recipe now
retries the whole install, three attempts a few seconds apart, and still fails
after the last.

Nothing about that loop is visible in a normal run: the first attempt almost
always succeeds, and a mutant with the `sleep` or the ceiling deleted reports
green until the next bad minute. So the loop is *run*, not read: these tests
point the recipe at a stub `pip` through `BENCH_PIP` and count invocations,
the pattern `test_third_party_download_retry.py` uses for the envtest fetch.
"""

import pathlib
import shutil
import stat
import sys
import tempfile
import time
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _run_make import run_make  # noqa: E402

#: The ceiling the Makefile declares, and the one the issue asked for: three
#: attempts. The default is asserted on rather than overridden, because the
#: count is the contract -- a retry that silently became one attempt is the
#: regression, and a test that passed its own ceiling could not see it.
DEFAULT_ATTEMPTS = 3

#: What the recipe hands pip after the substituted command; asserted on so a
#: stub that was reached with the wrong arguments cannot read as a pass.
INSTALL_ARGS = "install -e bench/ pytest pyyaml"

#: The distinctive halves of the recipe's two messages, matched on stderr so a
#: run that failed for another reason cannot read as the retry firing.
RETRY_NOTICE = "bench deps install attempt %d of %d failed; retrying"
EXHAUSTED_ERROR = "failed after %d attempts"
INVALID_TUNABLE_ERROR = "BENCH_DEPS_INSTALL_ATTEMPTS must be a whole number"

#: Comfortably above the slowest case: two attempts with a one-second pause.
MAKE_TIMEOUT_SECONDS = 60

#: Delay used by the timing test: one sleep, so the floor is real without
#: making the suite wait.
TIMING_DELAY_SECONDS = 1

#: A stub pip that records its arguments and fails every time, the way the
#: clone did on the 504.
ALWAYS_FAILING_STUB = """#!/bin/sh
echo "$*" >> "%s"
echo "error: RPC failed; HTTP 504 curl 22 The requested URL returned error: 504" >&2
exit 1
"""

#: A stub pip that fails until it has been called the given number of times,
#: then succeeds: a transient fault that clears.
FAILS_THEN_SUCCEEDS_STUB = """#!/bin/sh
echo "$*" >> "%s"
if [ "$(wc -l < "%s")" -lt %d ]; then
  echo "error: RPC failed; HTTP 504 curl 22 The requested URL returned error: 504" >&2
  exit 1
fi
exit 0
"""


class BenchDepsInstallRetryTest(unittest.TestCase):
    """The recipe is executed against a stub, not read."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="bench-deps-retry-")).resolve()
        self.stub = self.tmp / "pip-stub"
        self.calls = self.tmp / "calls"
        self.calls.write_text("")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_stub(self, script):
        self.stub.write_text(script)
        self.stub.chmod(self.stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _run(self, attempts=None, delay=0):
        """Run the real target with the stub in pip's place.

        `attempts=None` leaves the Makefile's default in force; anything else,
        the empty string included, is passed as a `VAR=value` override, which
        is how a developer or a workflow would set it.
        """
        args = [
            "-s",
            "test-bench-deps",
            "BENCH_PIP=%s" % self.stub,
            "BENCH_DEPS_RETRY_DELAY_SECONDS=%s" % delay,
        ]
        if attempts is not None:
            args.append("BENCH_DEPS_INSTALL_ATTEMPTS=%s" % attempts)
        return run_make(args, timeout=MAKE_TIMEOUT_SECONDS)

    def _calls(self):
        return self.calls.read_text().splitlines()

    def test_a_failing_install_is_attempted_three_times_then_fails(self):
        self._write_stub(ALWAYS_FAILING_STUB % self.calls)
        result = self._run()
        self.assertEqual(
            len(self._calls()),
            DEFAULT_ATTEMPTS,
            "the recipe should have retried up to the ceiling, stderr was:\n%s" % result.stderr,
        )
        self.assertNotEqual(result.returncode, 0, "exhausting the attempts must fail the target")
        for attempt in range(1, DEFAULT_ATTEMPTS):
            self.assertIn(RETRY_NOTICE % (attempt, DEFAULT_ATTEMPTS), result.stderr)
        self.assertIn(EXHAUSTED_ERROR % DEFAULT_ATTEMPTS, result.stderr)
        self.assertNotIn(
            RETRY_NOTICE % (DEFAULT_ATTEMPTS, DEFAULT_ATTEMPTS),
            result.stderr,
            "the last attempt must not announce a retry that never comes",
        )

    def test_it_stops_at_the_first_success(self):
        """Two failures then a success: the target exits zero after three calls."""
        self._write_stub(FAILS_THEN_SUCCEEDS_STUB % (self.calls, self.calls, DEFAULT_ATTEMPTS))
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._calls()), DEFAULT_ATTEMPTS, "it should stop at the first success")
        self.assertNotIn(EXHAUSTED_ERROR % DEFAULT_ATTEMPTS, result.stderr)

    def test_a_first_time_success_is_one_invocation_with_the_install_arguments(self):
        """The common case is unchanged: one pip call, the same arguments as before."""
        self._write_stub(FAILS_THEN_SUCCEEDS_STUB % (self.calls, self.calls, 1))
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._calls(), [INSTALL_ARGS])
        self.assertEqual(result.stderr, "", "a clean install must print no retry notice")

    def test_it_waits_between_attempts(self):
        """The pause, which asserting on recipe text cannot see.

        Two attempts and a one-second delay: one sleep, so a recipe with the
        `sleep` deleted returns in milliseconds and fails here.
        """
        self._write_stub(ALWAYS_FAILING_STUB % self.calls)
        started = time.monotonic()
        self._run(attempts=2, delay=TIMING_DELAY_SECONDS)
        elapsed = time.monotonic() - started
        self.assertEqual(len(self._calls()), 2)
        self.assertGreaterEqual(
            elapsed,
            float(TIMING_DELAY_SECONDS),
            "two attempts with a %ds delay must sleep at least once; took %.2fs"
            % (TIMING_DELAY_SECONDS, elapsed),
        )

    def test_an_empty_ceiling_fails_instead_of_looping(self):
        """An empty tunable is a stop, not an unbounded retry.

        `?=` does not defend against one: an exported empty variable, or
        `make ... BENCH_DEPS_INSTALL_ATTEMPTS=`, reaches the recipe. Without
        the guard `[ "$attempt" -ge "" ]` errors, `if` reads the error as
        false, and the loop retries until something outside it gives up.
        """
        self._write_stub(ALWAYS_FAILING_STUB % self.calls)
        result = self._run(attempts="")
        self.assertNotEqual(result.returncode, 0, "an empty ceiling must fail the target")
        self.assertIn(INVALID_TUNABLE_ERROR, result.stderr)
        self.assertEqual(
            len(self._calls()), 0, "it must refuse before installing, not after an attempt"
        )


if __name__ == "__main__":
    unittest.main()
