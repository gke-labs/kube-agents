"""Tests for the bounded image-build wait in hack/ci-deploy.sh section 4 (#3129).

On 2026-10-14 four smoke runs sat six hours in `gcloud builds submit`: the
command streams the build log from storage and returns when the stream ends,
and on two build nodes the stream stalled mid-push after Cloud Build had
already finished. Section 4 now submits with --async, prints the build id and
its Console URL first, polls `gcloud builds describe` for a terminal status
under a hard cap, and prints `gcloud builds log` only on failure.

Each test lifts the real section 4 text with the file-head constants and runs
it against a `gcloud` stub on PATH that answers `describe` from a scripted
sequence. What is pinned: SUCCESS on a later poll continues without reading
the log; a build that never reaches a terminal state exits at the cap naming
the build id (the test that keeps the loop from being simplified back into a
stream); FAILURE prints the log and exits non-zero; a `describe` call that
errors is polled again rather than failing the run; the region the worker
pool needs reaches every call; a build that ended failed on BuildKit's
"flightcontrol: exceeded retry timeout" is submitted once more, and no other
failure is (#3911).
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"

_BUILD_START = "# ─── 4. Build Container Images"
_BUILD_END = "# ─── 5. Chart Deployment"

_BUILD_ID = "2110451680007950336"
_BUILD_URL = f"https://console.cloud.google.com/cloud-build/builds/{_BUILD_ID}?project=1"
_LOG_LINE = "Step #1: ERROR: failed to push"
# What the second submit returns, so a test can tell the two builds apart.
_RETRY_BUILD_ID = "2110451680007950337"
_FLIGHTCONTROL_LOG_LINE = "Step #1: #37 ERROR: flightcontrol: exceeded retry timeout"
_RETRY_WARNING = "#3911"

# Answers `builds submit` with the id and URL the real --format prints, one
# tab between (a second submit gets _RETRY_BUILD_ID); `builds describe` with the next status in
# GCLOUD_DESCRIBE_SEQUENCE (the last one repeats; ERR exits 1 with no
# status); `builds log` with GCLOUD_LOG_LINE, or one recognisable line. Every call's argv is
# appended to GCLOUD_CALLS.
_GCLOUD_STUB = f"""#!/bin/bash
echo "$*" >> "${{GCLOUD_CALLS}}"
case "$1 $2" in
  "builds submit")
    id={_BUILD_ID}
    [ "$(grep -c '^builds submit' "${{GCLOUD_CALLS}}")" -gt 1 ] && id={_RETRY_BUILD_ID}
    printf '%s\\t{_BUILD_URL}\\n' "${{id}}"; exit 0 ;;
  "builds describe")
    n="$(grep -c '^builds describe' "${{GCLOUD_CALLS}}")"
    set -- ${{GCLOUD_DESCRIBE_SEQUENCE}}
    [ "${{n}}" -gt "$#" ] && n="$#"
    status="$(eval "echo \\${{${{n}}}}")"
    if [ "${{status}}" = "ERR" ]; then echo "ERROR: (gcloud.builds.describe) connection reset" >&2; exit 1; fi
    echo "${{status}}"; exit 0 ;;
  "builds log") echo "${{GCLOUD_LOG_LINE:-{_LOG_LINE}}}"; exit 0 ;;
esac
exit 0
"""


def _text():
    return _CI_DEPLOY.read_text(encoding="utf-8")


def _constants_block():
    return "\n".join(line for line in _text().splitlines() if line.startswith("readonly "))


def _build_section():
    text = _text()
    start = text.find(_BUILD_START)
    assert start != -1, f"{_BUILD_START!r} not found in hack/ci-deploy.sh"
    end = text.find(_BUILD_END, start)
    assert end != -1, f"{_BUILD_END!r} not found after {_BUILD_START!r}"
    return text[start:end]


class CiDeployImageBuildTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        stub = self.bin_dir / "gcloud"
        stub.write_text(_GCLOUD_STUB)
        stub.chmod(0o755)
        self.calls = root / "gcloud-calls"
        self.calls.write_text("")

    def tearDown(self):
        self.tmp.cleanup()

    # A test that reaches a terminal status never waits on the cap, so the
    # default only has to outlast a slow runner: at 2 s, a loaded runner hit it
    # after two ERR polls and failed "ERR ERR SUCCESS". The cap test passes 1.
    def _run(self, describe_sequence, cap_seconds=60, region_args="", max_errors=20, log_line=_LOG_LINE):
        script = "\n".join(
            [
                "set -euo pipefail",
                "unset RC_COMMIT_SHA",
                'export AR_REPO="us-central1-docker.pkg.dev/p/r"',
                'export TAG="pr-1-abc"',
                'export PROJECT_ID="kube-agents-evals-36"',
                'export HERMES_AGENT_TAG="v0"',
                "BUILD_WORKER_ARGS=(--machine-type=e2-highcpu-8)",
                region_args,
                "A2A_OPERATOR_ENV_ARGS=()",
                # Section 2b-ii admits the bridge concurrency; section 4 hands it to the operator.
                'MODE_NEXT_BRIDGE_CONCURRENCY="4"',
                _constants_block(),
                _build_section(),
                'echo "REACHED SECTION 5"',
            ]
        )
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}:{os.environ['PATH']}",
            "GCLOUD_CALLS": str(self.calls),
            "GCLOUD_DESCRIBE_SEQUENCE": describe_sequence,
            "GCLOUD_LOG_LINE": log_line,
            # The real values are 4200 s, 15 s and 60 s; the tests shrink the
            # poll to zero, and the cap test shrinks the cap, so a
            # never-terminal build ends in seconds.
            "CI_BUILD_WAIT_CAP_SECONDS": str(cap_seconds),
            "CI_BUILD_POLL_INTERVAL_SECONDS": "0",
            "CI_BUILD_CALL_TIMEOUT_SECONDS": "5",
            "CI_BUILD_DESCRIBE_MAX_CONSECUTIVE_ERRORS": str(max_errors),
        }
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=120,
        )

    def _calls(self, prefix):
        return [ln for ln in self.calls.read_text().splitlines() if ln.startswith(prefix)]

    def test_success_on_a_later_poll_continues_without_reading_the_log(self):
        result = self._run("QUEUED WORKING SUCCESS")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("REACHED SECTION 5", result.stdout)
        self.assertIn("✓ Container image builds finished", result.stdout)
        self.assertEqual(len(self._calls("builds describe")), 3, self.calls.read_text())
        self.assertEqual(self._calls("builds log"), [], "the log is read only on failure")
        submit = self._calls("builds submit")
        self.assertEqual(len(submit), 1, self.calls.read_text())
        self.assertIn(" --async", submit[0], "the submit must not wait on the log stream")
        # The id and URL go out before the first poll, so a run killed while
        # polling still names the build it was waiting for.
        self.assertIn(_BUILD_ID, result.stdout)
        self.assertIn(_BUILD_URL, result.stdout)
        self.assertLess(result.stdout.index(_BUILD_ID), result.stdout.index("✓ Container image builds finished"))

    def test_a_build_that_never_reaches_a_terminal_state_exits_at_the_cap_naming_the_build(self):
        result = self._run("WORKING", cap_seconds=1)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("REACHED SECTION 5", result.stdout)
        combined = result.stdout + result.stderr
        self.assertIn(_BUILD_ID, combined)
        self.assertRegex(combined, r"(?i)cap|did not reach a terminal|within 1s")
        self.assertGreaterEqual(len(self._calls("builds describe")), 1, self.calls.read_text())
        self.assertEqual(len(self._calls("builds log")), 1, "the log is printed on the way out")
        self.assertEqual(len(self._calls("builds cancel")), 1, "a build the loop gave up on is cancelled")

    def test_a_failed_build_prints_its_log_and_exits_nonzero(self):
        result = self._run("WORKING FAILURE")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        combined = result.stdout + result.stderr
        self.assertIn("FAILURE", combined)
        self.assertIn(_BUILD_ID, combined)
        self.assertIn(_LOG_LINE, result.stdout, "gcloud builds log output must reach the job log")
        self.assertNotIn("REACHED SECTION 5", result.stdout)
        self.assertEqual(self._calls("builds cancel"), [], "a build that ended needs no cancel")

    def test_a_describe_that_errors_is_polled_again(self):
        result = self._run("ERR ERR SUCCESS")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self._calls("builds describe")), 3, self.calls.read_text())
        self.assertIn("REACHED SECTION 5", result.stdout)

    def test_a_describe_that_keeps_erroring_fails_the_run_before_the_cap(self):
        # A bad region or a revoked permission errors every time; that ends the
        # run after the consecutive-error bound, not after 70 minutes.
        result = self._run("ERR", cap_seconds=600, max_errors=3)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self._calls("builds describe")), 3, self.calls.read_text())
        self.assertIn("3 times in a row", result.stdout + result.stderr)
        # The warning names the call's real exit status, not the counter's.
        self.assertIn("describe 2110451680007950336 exited 1 (", result.stdout)
        self.assertIn(_BUILD_ID, result.stdout + result.stderr)
        self.assertEqual(len(self._calls("builds log")), 1)
        self.assertEqual(len(self._calls("builds cancel")), 1)

    def test_describe_and_log_carry_the_worker_pools_region(self):
        # The submit's region rides in BUILD_WORKER_ARGS as before; the two
        # new calls take it from BUILD_REGION_ARGS, which section 2c derives
        # beside it.
        result = self._run("FAILURE", region_args="BUILD_REGION_ARGS=(--region=us-central1)")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        for prefix in ("builds describe", "builds log"):
            for call in self._calls(prefix):
                self.assertIn("--region=us-central1", call, call)
            self.assertGreaterEqual(len(self._calls(prefix)), 1, prefix)

    def test_a_flightcontrol_failure_is_submitted_once_more_and_can_succeed(self):
        result = self._run("WORKING FAILURE SUCCESS", log_line=_FLIGHTCONTROL_LOG_LINE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("REACHED SECTION 5", result.stdout)
        self.assertEqual(len(self._calls("builds submit")), 2, self.calls.read_text())
        self.assertIn(_FLIGHTCONTROL_LOG_LINE, result.stdout, "the first build's log still reaches the job log")
        warning = [ln for ln in result.stderr.splitlines() if ln.startswith("WARNING:") and _RETRY_WARNING in ln]
        self.assertEqual(len(warning), 1, result.stderr)
        self.assertIn(_BUILD_ID, warning[0], "the warning names the build that failed")
        self.assertIn("attempt 1 of 2", warning[0])
        # The second build is the one polled to SUCCESS.
        self.assertIn(_RETRY_BUILD_ID, result.stdout)
        self.assertIn(_RETRY_BUILD_ID, self._calls("builds describe")[-1])
        self.assertEqual(self._calls("builds cancel"), [], "a build that ended needs no cancel")

    def test_a_second_flightcontrol_failure_fails_the_run(self):
        result = self._run("FAILURE", log_line=_FLIGHTCONTROL_LOG_LINE)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("REACHED SECTION 5", result.stdout)
        self.assertEqual(len(self._calls("builds submit")), 2, self.calls.read_text())
        self.assertEqual(len(self._calls("builds log")), 2, self.calls.read_text())
        self.assertIn(_RETRY_BUILD_ID, result.stderr, "the error names the second build")
        self.assertIn("again, on the one resubmit", result.stderr)

    def test_the_resubmit_repeats_the_first_submit_exactly(self):
        self._run("FAILURE", log_line=_FLIGHTCONTROL_LOG_LINE)
        submits = self._calls("builds submit")
        self.assertEqual(len(submits), 2, self.calls.read_text())
        self.assertEqual(submits[0], submits[1])

    def test_a_near_miss_of_the_flightcontrol_line_is_not_submitted_again(self):
        result = self._run("FAILURE", log_line="Step #1: #37 ERROR: flightcontrol: retry timeout")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self._calls("builds submit")), 1, self.calls.read_text())

    def test_a_build_that_ended_other_than_failure_is_not_submitted_again(self):
        # TIMEOUT, CANCELLED and the rest ended for another reason, even when
        # an earlier step logged the line and recovered.
        for status in ("TIMEOUT", "CANCELLED", "INTERNAL_ERROR"):
            with self.subTest(status=status):
                self.calls.write_text("")
                result = self._run(status, log_line=_FLIGHTCONTROL_LOG_LINE)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(len(self._calls("builds submit")), 1, self.calls.read_text())

    def test_describe_errors_are_not_submitted_again_even_with_flightcontrol_in_the_log(self):
        result = self._run("ERR", max_errors=2, log_line=_FLIGHTCONTROL_LOG_LINE)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self._calls("builds submit")), 1, self.calls.read_text())
        self.assertEqual(len(self._calls("builds cancel")), 1, self.calls.read_text())

    def test_a_failure_without_flightcontrol_is_not_submitted_again(self):
        result = self._run("FAILURE")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self._calls("builds submit")), 1, self.calls.read_text())
        self.assertNotIn(_RETRY_WARNING, result.stdout + result.stderr)

    def test_a_cap_timeout_is_not_submitted_again_even_with_flightcontrol_in_its_log(self):
        # Only a build that ended failed is a flightcontrol flake; one the loop
        # gave up on is cancelled and fails the run as before.
        result = self._run("WORKING", cap_seconds=1, log_line=_FLIGHTCONTROL_LOG_LINE)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self._calls("builds submit")), 1, self.calls.read_text())
        self.assertEqual(len(self._calls("builds cancel")), 1, self.calls.read_text())

    def test_the_region_args_are_derived_before_section_4(self):
        text = _text()
        self.assertIn("BUILD_REGION_ARGS=(", text)
        self.assertLess(text.index("BUILD_REGION_ARGS=("), text.index(_BUILD_START))


if __name__ == "__main__":
    unittest.main(verbosity=2)
