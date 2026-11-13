"""Test support: keep a test module from running the real gcloud.

A test that leaves a gcloud call unstubbed used to run the gcloud on `PATH`
with the developer's login, and made real GCP calls. `install()` closes that
for a whole test module: a stand-in `gcloud` is first on `PATH`, and it records
its arguments and fails.

`Guard.uninstall()` restores `PATH`, and raises when the stand-in was called: a
test, or a code path it reached, tried to run gcloud. A test stubs the call
with a gcloud of its own ahead of the stand-in on `PATH`, or with a mock.

The stand-in reaches a gcloud started with the `PATH` the guard set. A call
that passes a `PATH` of its own without the guard's entry, or names gcloud by
an absolute path, does not get it, and the guard does not see it. Nor does it
see the calls after a test that replaces `PATH` in `os.environ` and leaves it.

One guard at a time: a second `install()` before the first `uninstall()` is
not supported. A module whose tests run a suite of the same module, which
runs its fixtures again, needs the holder count `no_live_kubectl` has.

    def setUpModule():
        global _NO_GCLOUD
        _NO_GCLOUD = no_live_gcloud.install()

    def tearDownModule():
        _NO_GCLOUD.uninstall()

Used by tests only; nothing at runtime imports it.
"""

import os
import shutil
import tempfile
from pathlib import Path

GCLOUD = "gcloud"
PATH_ENV = "PATH"

# The guard's temporary directory and the names inside it.
_DIR_PREFIX = "no-live-gcloud-"
_CALLS_FILE = "calls"
_BIN_DIR = "bin"

# The stand-in's exit code: not one gcloud itself returns, so a test that
# reads it knows which of the two answered.
STAND_IN_EXIT_CODE = 97
STAND_IN_MESSAGE = "gcloud is not available in unit tests (no_live_gcloud)"
STAND_IN_MODE = 0o755
# One NUL-ended record a call: an argument can hold a newline.
_CALL_END = "\0"
_STAND_IN_SCRIPT = """#!/bin/sh
printf '%s\\0' "$*" >> "{calls}"
echo "{message}" >&2
exit {code}
"""

# How many recorded calls the failure message quotes.
REPORTED_CALLS = 5


class Guard:
    """The stand-in on `PATH`, from construction until `uninstall()`."""

    def __init__(self):
        self._dir = Path(tempfile.mkdtemp(prefix=_DIR_PREFIX))
        self._calls = self._dir / _CALLS_FILE
        self._calls.touch()
        self.bin_dir = self._dir / _BIN_DIR
        self.bin_dir.mkdir()
        self.stand_in = self.bin_dir / GCLOUD
        self.stand_in.write_text(
            _STAND_IN_SCRIPT.format(calls=self._calls, message=STAND_IN_MESSAGE, code=STAND_IN_EXIT_CODE),
            encoding="utf-8",
        )
        self.stand_in.chmod(STAND_IN_MODE)
        self._saved_path = os.environ.get(PATH_ENV)
        os.environ[PATH_ENV] = str(self.bin_dir) + os.pathsep + (self._saved_path or "")

    def calls(self) -> list[str]:
        """The arguments of each call the stand-in took, oldest first."""
        # errors="replace": an argument need not be UTF-8, and `uninstall()`
        # must still get to its cleanup and its report.
        recorded = self._calls.read_text(encoding="utf-8", errors="replace")
        return recorded.split(_CALL_END)[:-1]

    def uninstall(self) -> None:
        if self._saved_path is None:
            os.environ.pop(PATH_ENV, None)
        else:
            os.environ[PATH_ENV] = self._saved_path
        calls = self.calls()
        shutil.rmtree(self._dir, ignore_errors=True)
        if calls:
            shown = "; ".join(f"{GCLOUD} {call}" for call in calls[:REPORTED_CALLS])
            raise AssertionError(
                f"{len(calls)} gcloud call(s) left this test module for the gcloud on PATH. "
                f"Stub the call in the test that makes it. First calls: {shown}"
            )


def install() -> Guard:
    return Guard()


def assert_in_force(test, guard: Guard) -> None:
    """For one test in each guarded module: the stand-in is the gcloud on `PATH`."""
    test.assertEqual(shutil.which(GCLOUD), str(guard.stand_in))
