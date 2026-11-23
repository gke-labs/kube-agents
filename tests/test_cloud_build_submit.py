"""Unit tests for .github/scripts/cloud_build_submit.sh.

docker-publish-gcp.yml runs every Cloud Build through it. It submits once more
only when the build died on BuildKit's "flightcontrol: exceeded retry timeout"
(#3911), at most once, and otherwise exits with gcloud's own status. A fake
`gcloud` on PATH plays each outcome from a script file, one line per call.
"""

import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "cloud_build_submit.sh"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "docker-publish-gcp.yml"

_FLIGHTCONTROL = "Step #1: #37 ERROR: flightcontrol: exceeded retry timeout"
_OTHER_FAILURE = "Step #1: #12 ERROR: failed to solve: process did not complete successfully"
_GCLOUD_FAIL_EXIT = 1
_USAGE_EXIT = 2
_RETRY_WARNING = "::warning title=Cloud Build retried (#3911)::"

# Each line of the plan is "<exit code> <message>"; call N plays line N.
_FAKE_GCLOUD = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    set -euo pipefail
    echo "$*" >> "$FAKE_CALLS"
    n=$(wc -l < "$FAKE_CALLS" | tr -d ' ')
    line=$(sed -n "${n}p" "$FAKE_PLAN")
    code=${line%% *}
    msg=${line#* }
    echo "$msg"
    exit "$code"
    """
)


class CloudBuildSubmitTest(unittest.TestCase):
    def run_script(self, plan_lines, args=("builds-arg", ".")):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            gcloud = bin_dir / "gcloud"
            gcloud.write_text(_FAKE_GCLOUD)
            gcloud.chmod(0o755)
            plan = tmp_path / "plan"
            plan.write_text("".join(f"{line}\n" for line in plan_lines))
            calls = tmp_path / "calls"
            calls.write_text("")
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
            env["FAKE_PLAN"] = str(plan)
            env["FAKE_CALLS"] = str(calls)
            result = subprocess.run(
                ["bash", str(_SCRIPT), *args],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            return result, calls.read_text().splitlines()

    def test_a_green_build_is_submitted_once(self):
        result, calls = self.run_script(["0 build ok"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ["builds submit builds-arg ."])
        self.assertNotIn(_RETRY_WARNING, result.stdout)

    def test_a_flightcontrol_death_is_submitted_again_and_warned(self):
        result, calls = self.run_script([f"{_GCLOUD_FAIL_EXIT} {_FLIGHTCONTROL}", "0 build ok"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertIn(_RETRY_WARNING, result.stdout)
        # The first attempt's log still reaches the job.
        self.assertIn(_FLIGHTCONTROL, result.stdout)

    def test_a_second_flightcontrol_death_fails_without_a_third_submit(self):
        result, calls = self.run_script(
            [f"{_GCLOUD_FAIL_EXIT} {_FLIGHTCONTROL}", f"{_GCLOUD_FAIL_EXIT} {_FLIGHTCONTROL}", "0 never reached"]
        )
        self.assertEqual(result.returncode, _GCLOUD_FAIL_EXIT)
        self.assertEqual(len(calls), 2)

    def test_any_other_failure_is_not_retried(self):
        result, calls = self.run_script([f"{_GCLOUD_FAIL_EXIT} {_OTHER_FAILURE}", "0 never reached"])
        self.assertEqual(result.returncode, _GCLOUD_FAIL_EXIT)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(_RETRY_WARNING, result.stdout)

    def test_gclouds_own_exit_status_is_kept(self):
        odd_exit = 7
        result, calls = self.run_script([f"{odd_exit} {_OTHER_FAILURE}"])
        self.assertEqual(result.returncode, odd_exit)
        self.assertEqual(len(calls), 1)

    def test_no_arguments_is_a_usage_error(self):
        result, calls = self.run_script(["0 never reached"], args=())
        self.assertEqual(result.returncode, _USAGE_EXIT)
        self.assertEqual(calls, [])


class PublishWorkflowUsesTheWrapperTest(unittest.TestCase):
    def test_every_cloud_build_in_the_gcp_publish_goes_through_the_wrapper(self):
        text = _WORKFLOW.read_text()
        self.assertNotIn("gcloud builds submit", text.replace("`gcloud builds submit`", ""))
        self.assertGreater(text.count(".github/scripts/cloud_build_submit.sh"), 1)


if __name__ == "__main__":
    unittest.main()
