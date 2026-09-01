"""Unit tests for scripts/release/teardown_environment.sh.

The post-run teardown is the only thing that removes the RC cluster on a run
that passed, so its failure handling is the opposite of the pre-install
teardown's: there is no later step to compensate, and RC_TEARDOWN_STRICT does
not apply. These pin that difference, and the argument forwarding that decides
which project gets destroyed.
"""

import pathlib
import subprocess
import tempfile
import unittest

from tests.testing.common import (
    TRUTHY_BOOLEAN_INPUTS,
    get_isolated_test_env,
)
from tests.testing.release import (
    MOCK_CALLS_LOG,
    MOCK_GCP_PROJECT_ID,
    MOCK_GCP_REGION,
    MOCK_GKE_CLUSTER_NAME,
    MOCK_UNINSTALL_FAIL_SIGNAL,
    MOCK_UNINSTALL_SCRIPT,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TEARDOWN_SCRIPT = _REPO_ROOT / "scripts" / "release" / "teardown_environment.sh"


class TeardownEnvironmentTest(unittest.TestCase):
    def _run(self, uninstall_exit, extra_env=None, uninstall_stdout=""):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_dir = pathlib.Path(tmp.name)

        recorded_calls = tmp_dir / MOCK_CALLS_LOG
        summary = tmp_dir / "step_summary.md"
        summary.touch()

        mock_uninstall = tmp_dir / MOCK_UNINSTALL_SCRIPT
        # Quoted heredoc for the same reason as the provision suite's: the
        # fence-escape test feeds backticks and HTML through here, and an
        # `echo "…"` would let the mock's own shell interpret them first.
        mock_uninstall.write_text(f"""#!/usr/bin/env bash
echo "{MOCK_UNINSTALL_FAIL_SIGNAL} $*" >> "{recorded_calls}"
cat <<'UNINSTALL_STDOUT_EOF'
{uninstall_stdout}
UNINSTALL_STDOUT_EOF
exit {uninstall_exit}
""")
        mock_uninstall.chmod(0o755)

        env = get_isolated_test_env(
            overrides={
                "GCP_PROJECT_ID": MOCK_GCP_PROJECT_ID,
                "GCP_REGION": MOCK_GCP_REGION,
                "GKE_CLUSTER_NAME": MOCK_GKE_CLUSTER_NAME,
                # get_isolated_test_env strips GITHUB_*, so the job-summary path
                # only exists when a test asks for it.
                "GITHUB_STEP_SUMMARY": str(summary),
                **(extra_env or {}),
            }
        )
        proc = subprocess.run(
            ["bash", str(_TEARDOWN_SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(tmp_dir),
        )
        calls = recorded_calls.read_text().splitlines() if recorded_calls.exists() else []
        return proc, calls, summary.read_text()

    def test_fails_when_required_env_vars_missing(self):
        """set -u aborts before the script creates a temp file or calls out."""
        proc = subprocess.run(
            ["bash", str(_TEARDOWN_SCRIPT)],
            capture_output=True,
            text=True,
            env={},
            cwd=str(_REPO_ROOT),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unbound variable", proc.stderr)

    def test_forwards_the_rc_coordinates_to_uninstall(self):
        proc, calls, _ = self._run(uninstall_exit=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(calls), 1, calls)
        for expected in (
            "--non-interactive",
            "-y",
            f"--project-id={MOCK_GCP_PROJECT_ID}",
            f"--region={MOCK_GCP_REGION}",
            f"--cluster-name={MOCK_GKE_CLUSTER_NAME}",
        ):
            self.assertIn(expected, calls[0])

    def test_a_clean_teardown_reports_the_cluster_gone(self):
        proc, _, summary = self._run(uninstall_exit=0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Teardown complete", proc.stdout)
        self.assertNotIn("::error", proc.stdout + proc.stderr)
        self.assertEqual(summary, "")

    def test_nothing_to_tear_down_is_not_a_failure(self):
        # Exit 3 is surprising here — step 2 installed against this target
        # minutes earlier — but it still means nothing is left running, which is
        # all this job promises.
        proc, _, summary = self._run(uninstall_exit=3)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Nothing to tear down", proc.stdout)
        self.assertNotIn("::error", proc.stdout + proc.stderr)
        self.assertEqual(summary, "")

    def test_a_failed_teardown_is_fatal(self):
        """The opposite of the pre-install teardown, which warns and continues.

        Nothing runs after this job, so a warning would leave a GKE cluster
        billing with a green pipeline over it.
        """
        proc, _, summary = self._run(
            uninstall_exit=1, uninstall_stdout="destroy blew up here"
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("::error title=Environment teardown failed::", proc.stderr)
        self.assertIn("STILL RUNNING", proc.stderr)
        self.assertIn("still running (exit 1)", summary)
        self.assertIn("destroy blew up here", summary)

    def test_the_exit_status_is_uninstalls_own(self):
        proc, _, _ = self._run(uninstall_exit=4)
        self.assertEqual(proc.returncode, 4, proc.stdout)

    def test_strict_mode_does_not_make_a_failure_survivable(self):
        """RC_TEARDOWN_STRICT governs the pre-install teardown, not this one.

        It decides whether to install on top of an environment that survived. A
        surviving environment here has nothing installing over it, so the
        variable must not be readable as permission to exit 0.
        """
        for value in [*TRUTHY_BOOLEAN_INPUTS, "false", "off", "0", ""]:
            with self.subTest(value=value):
                proc, _, _ = self._run(
                    uninstall_exit=1, extra_env={"RC_TEARDOWN_STRICT": value}
                )
                self.assertEqual(proc.returncode, 1, proc.stdout)

    def test_teardown_output_cannot_break_out_of_the_summary_fence(self):
        proc, _, summary = self._run(
            uninstall_exit=1,
            uninstall_stdout="oops ``` <img src=x onerror=alert(1)>",
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        # Exactly the two fences the script writes, so nothing in the captured
        # output closed the block early and rendered the rest as HTML.
        self.assertEqual(summary.count("```"), 2)
        self.assertIn("oops", summary)

    def test_the_summary_names_the_cluster_to_remove_by_hand(self):
        _, _, summary = self._run(uninstall_exit=1)
        self.assertIn(f"--project-id={MOCK_GCP_PROJECT_ID}", summary)
        self.assertIn(f"--cluster-name={MOCK_GKE_CLUSTER_NAME}", summary)


if __name__ == "__main__":
    unittest.main()
