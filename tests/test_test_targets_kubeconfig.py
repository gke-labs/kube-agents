"""The test targets outside the sweep give their tests an empty KUBECONFIG.

    python3 -m unittest discover -s tests -p 'test_test_targets_kubeconfig.py'

`make test-integration` and the operator's `make test-python` run test files
that the `make test-python` sweep also runs, but in one process and outside
the sweep's worker. The worker gives each file an empty kubeconfig of its own
(tests/test_python_test_sweep.py pins that), so a kubectl or helm call a test
leaves unstubbed finds no cluster on a developer's machine. These two targets
have to do the same, or the convenience run reads the developer's kubeconfig
where the sweep does not.

A stand-in `python3`, first on PATH, takes the place of the test run: it
records the KUBECONFIG the recipe gave it and exits with the status the test
names. No test suite runs.
"""

import os
import pathlib
import stat
import sys
import tempfile
import unittest

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from _run_make import REPO_ROOT, run_make  # noqa: E402

#: Each target and the directory its Makefile is in.
TARGETS = (
    ("test-integration", REPO_ROOT),
    ("test-python", REPO_ROOT / "k8s-operator"),
)

#: These recipes only start the stand-in, but the sweep this suite runs inside
#: can be slow.
MAKE_TIMEOUT_SECONDS = 120

STAND_IN = """#!/bin/sh
# Stands in for the test run: record the kubeconfig, then exit as told.
if [ -f "$KUBECONFIG" ] && [ ! -s "$KUBECONFIG" ]; then state=empty-file; else state=other; fi
printf 'path=%s\\nstate=%s\\n' "$KUBECONFIG" "$state" > "$STAND_IN_RECORD"
exit "$STAND_IN_STATUS"
"""
INHERITED_KUBECONFIG = "current-context: a-developers-cluster\n"
FAILING_STATUS = 7


def run_target(target, directory, status=0):
    """Run `target` with the stand-in for python3 and a kubeconfig exported.

    Returns the completed make, what the stand-in recorded, and the exported
    kubeconfig's path and content after the run.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        stand_in = bin_dir / "python3"
        stand_in.write_text(STAND_IN)
        stand_in.chmod(stand_in.stat().st_mode | stat.S_IEXEC)
        inherited = tmp / "inherited-kubeconfig"
        inherited.write_text(INHERITED_KUBECONFIG)
        record = tmp / "record"
        done = run_make(
            [target],
            timeout=MAKE_TIMEOUT_SECONDS,
            cwd=directory,
            extra_env={
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "KUBECONFIG": str(inherited),
                "STAND_IN_RECORD": str(record),
                "STAND_IN_STATUS": str(status),
            },
        )
        if not record.exists():
            raise AssertionError(f"make {target} never started python3:\n{done.stdout}{done.stderr}")
        seen = dict(line.split("=", 1) for line in record.read_text().splitlines())
        return done, seen, str(inherited), inherited.read_text()


class TargetKubeconfigTest(unittest.TestCase):
    def test_the_tests_get_an_empty_kubeconfig_that_is_not_the_exported_one(self):
        for target, directory in TARGETS:
            with self.subTest(target=target):
                done, seen, inherited, inherited_text = run_target(target, directory)
                self.assertEqual(0, done.returncode, done.stdout + done.stderr)
                self.assertEqual("empty-file", seen["state"], seen)
                self.assertNotEqual(inherited, seen["path"])
                self.assertEqual(INHERITED_KUBECONFIG, inherited_text)

    def test_the_kubeconfig_is_gone_after_the_run(self):
        for target, directory in TARGETS:
            with self.subTest(target=target):
                _, seen, _, _ = run_target(target, directory)
                self.assertFalse(pathlib.Path(seen["path"]).exists(), seen["path"])

    def test_a_failing_test_run_still_fails_the_target(self):
        """The cleanup must not replace the test run's status with its own."""
        for target, directory in TARGETS:
            with self.subTest(target=target):
                done, seen, _, _ = run_target(target, directory, status=FAILING_STATUS)
                self.assertNotEqual(0, done.returncode, done.stdout + done.stderr)
                self.assertFalse(pathlib.Path(seen["path"]).exists(), seen["path"])


if __name__ == "__main__":
    unittest.main()
