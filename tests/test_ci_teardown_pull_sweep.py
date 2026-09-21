"""Teardown's Step 5 closes the agent's leftovers, and can never red the job.

`hack/ci-teardown.sh` grew a step that talks to GitHub rather than to the
cluster: it closes the pull requests the platform agent left in the leased
project's GitOps repository, so the next lease of that project does not inherit
them (#1755). The Prow wrapper runs this script at job start as well as at job
end, which is why the sweep lives here and not on a timer — a lease begins
clean even when the run before it was hard-killed.

Four properties, none of them visible from a successful run:

* it skips, successfully and out loud, when no App key is mounted or the
  project maps to no repository. A laptop run of this script must not need a
  GitHub credential, and a project mid-onboarding must not turn its teardown
  red;
* it passes the leased project's own repository, resolved through
  `gitops_repo_for_project()` in hack/ci-deploy.sh — the mapping's one home,
  lifted by its own text rather than copied here;
* a failed sweep is counted and named in the summary, and still does not change
  the teardown's exit code. The Prow wrapper has a Boskos release to reach;
* it is reachable when every step before it has failed, which is the aborted
  run this whole file exists for.

The steps are lifted from the script's own text and run under bash with a
stubbed python3, the same approach as tests/test_ci_teardown_sweep.py. The lift
starts after the context guards: those are must-not-touch-the-wrong-cluster
exits, the one path where the sweep is supposed to be unreachable.
"""

import pathlib
import re
import stat
import subprocess
import tempfile
import unittest

from tests.testing.common import get_isolated_test_env

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_TEARDOWN = _REPO_ROOT / "hack" / "ci-teardown.sh"
_CI_EVAL_PR = _REPO_ROOT / "hack" / "ci-eval-pr.sh"

_STEPS_START = "START_TIME=$SECONDS"
_STEP_4_HEADER = "Step 4: Sweeping namespaced kube-agents resources"
_STEP_5_HEADER = "Step 5: Closing the agent's leftover pull requests"

# A real row of the mapping, and a string that will never be one. The
# fail-closed fixture is deliberately not the next name in the sequence: that
# is what kube-agents-evals-3 taught tests/test_ci_gitops_repo.py.
_MAPPED_PROJECT = "kube-agents-evals-7"
_MAPPED_REPO = "gke-agentic/kube-agents-evals-7-infra"
_UNMAPPED_PROJECT = "not-a-pool-project-fixture"


def _teardown_steps():
    """The lifted steps, prefixed with the file-head `readonly` constants."""
    text = _CI_TEARDOWN.read_text(encoding="utf-8")
    start = text.find(_STEPS_START)
    assert start != -1, f"{_STEPS_START!r} not found in hack/ci-teardown.sh"
    constants = "\n".join(
        line for line in text[:start].splitlines() if line.startswith("readonly ")
    )
    return constants + "\n" + text[start:]


class CiTeardownPullSweepTest(unittest.TestCase):
    maxDiff = None

    def _run_steps(self, project=_MAPPED_PROJECT, key_file="", python_exit=0, strict=None):
        """Run the lifted steps with stubbed kubectl, helm and python3.

        Returns (returncode, stdout, python3 argv lines). The python3 stub
        records its arguments, so a test can assert what the sweep was asked to
        do without reaching GitHub.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            log = tmp_path / "python3.log"
            log.touch()
            for tool, code, record in (
                ("kubectl", 0, False),
                ("helm", 0, False),
                ("python3", python_exit, True),
            ):
                stub = bin_dir / tool
                stub.write_text(
                    "#!/usr/bin/env bash\n"
                    + (f'echo "$*" >> "{log}"\n' if record else "")
                    + f"exit {code}\n",
                    encoding="utf-8",
                )
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            overrides = {
                "NAMESPACE": "kubeagents-system",
                "PROJECT_ID": project,
                "SCRIPT_DIR": str(_REPO_ROOT / "hack"),
                "EVAL_LEDGER_APP_KEY_FILE": key_file,
            }
            if strict is not None:
                overrides["CI_TEARDOWN_STRICT"] = strict
            proc = subprocess.run(
                ["bash", "-c", "set -uo pipefail\n" + _teardown_steps()],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
                env=get_isolated_test_env(bin_dir=bin_dir, overrides=overrides),
            )
            calls = log.read_text(encoding="utf-8").splitlines()
        return proc.returncode, proc.stdout, calls

    # --- skipping ---------------------------------------------------------

    def test_no_mounted_key_skips_without_calling_anything(self):
        rc, out, calls = self._run_steps(key_file="")
        self.assertEqual(rc, 0, out)
        self.assertIn("skipped, no App key mounted", out)
        self.assertEqual(calls, [])

    def test_an_unmapped_project_skips_and_names_itself(self):
        rc, out, calls = self._run_steps(project=_UNMAPPED_PROJECT, key_file="/etc/k/key.pem")
        self.assertEqual(rc, 0, out)
        self.assertIn(_UNMAPPED_PROJECT, out)
        self.assertIn("maps to no GitOps repo", out)
        self.assertEqual(calls, [])

    def test_a_skip_is_not_counted_as_a_failure(self):
        rc, out, _ = self._run_steps(key_file="")
        self.assertEqual(rc, 0, out)
        self.assertIn("Cleanup Complete", out)
        self.assertNotIn("FAILED STEP", out)

    # --- the call it makes ------------------------------------------------

    def test_the_sweep_gets_the_leased_projects_own_repository(self):
        rc, out, calls = self._run_steps(key_file="/etc/ledger-app-key/key.pem")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(calls), 1, calls)
        argv = calls[0].split()
        self.assertIn("--repo", argv)
        self.assertEqual(argv[argv.index("--repo") + 1], _MAPPED_REPO)
        self.assertEqual(argv[argv.index("--key-file") + 1], "/etc/ledger-app-key/key.pem")
        self.assertTrue(argv[0].endswith("hack/ci_sweep_agent_pulls.py"), argv[0])

    def test_the_app_id_default_matches_the_one_the_eval_mints_with(self):
        """One App, two callers, two files that do not read each other.

        hack/ci-eval-pr.sh mints the grading token from it and hack/
        ci-teardown.sh mints the sweep's; a default that drifts here points the
        sweep at an App whose key is not the one mounted, and the failure is a
        401 that says nothing about the cause.
        """
        teardown = _CI_TEARDOWN.read_text(encoding="utf-8")
        eval_pr = _CI_EVAL_PR.read_text(encoding="utf-8")
        sweep_default = re.search(
            r'^readonly SWEEP_APP_ID="\$\{EVAL_LEDGER_APP_ID:-([^}]+)\}"', teardown, re.M
        )
        eval_default = re.search(
            r'^export EVAL_LEDGER_APP_ID="\$\{EVAL_LEDGER_APP_ID:-([^}]+)\}"', eval_pr, re.M
        )
        self.assertIsNotNone(sweep_default, "no SWEEP_APP_ID default in hack/ci-teardown.sh")
        self.assertIsNotNone(eval_default, "no EVAL_LEDGER_APP_ID default in hack/ci-eval-pr.sh")
        self.assertEqual(sweep_default.group(1), eval_default.group(1))

    # --- failure is counted, never fatal ----------------------------------

    def test_a_failed_sweep_is_named_in_the_summary(self):
        rc, out, _ = self._run_steps(key_file="/etc/k/key.pem", python_exit=1)
        self.assertIn("Agent pull-request sweep", out)
        self.assertIn("FAILED STEP", out)
        self.assertIn(_MAPPED_REPO, out)

    def test_a_failed_sweep_does_not_change_the_exit_code(self):
        # The Prow wrapper still has a Boskos release to reach. The honest
        # signal is the ✗ line, not a nonzero status.
        rc, out, _ = self._run_steps(key_file="/etc/k/key.pem", python_exit=1)
        self.assertEqual(rc, 0, out)

    def test_strict_mode_still_reports_it(self):
        rc, out, _ = self._run_steps(key_file="/etc/k/key.pem", python_exit=1, strict="1")
        self.assertEqual(rc, 1, out)

    # --- placement --------------------------------------------------------

    def test_the_sweep_runs_after_the_cluster_steps(self):
        """It needs neither the cluster nor anything the steps above leave.

        Ordering it last keeps a GitHub outage from delaying the deletes that
        free the project for its next lease.
        """
        text = _CI_TEARDOWN.read_text(encoding="utf-8")
        self.assertLess(text.index(_STEP_4_HEADER), text.index(_STEP_5_HEADER))

    def test_it_is_reachable_when_every_earlier_step_failed(self):
        # The aborted-run case this file exists for. helm and kubectl both
        # failing must not stop the sweep of the pull requests.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            bin_dir = tmp_path / "bin"
            bin_dir.mkdir()
            log = tmp_path / "python3.log"
            log.touch()
            for tool, code in (("kubectl", 1), ("helm", 1)):
                stub = bin_dir / tool
                stub.write_text(f"#!/usr/bin/env bash\nexit {code}\n", encoding="utf-8")
                stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            stub = bin_dir / "python3"
            stub.write_text(
                f'#!/usr/bin/env bash\necho "$*" >> "{log}"\nexit 0\n', encoding="utf-8"
            )
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            proc = subprocess.run(
                ["bash", "-c", "set -uo pipefail\n" + _teardown_steps()],
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
                env=get_isolated_test_env(
                    bin_dir=bin_dir,
                    overrides={
                        "NAMESPACE": "kubeagents-system",
                        "PROJECT_ID": _MAPPED_PROJECT,
                        "SCRIPT_DIR": str(_REPO_ROOT / "hack"),
                        "EVAL_LEDGER_APP_KEY_FILE": "/etc/k/key.pem",
                    },
                ),
            )
            calls = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1, proc.stdout)
        self.assertIn(_MAPPED_REPO, calls[0])


if __name__ == "__main__":
    unittest.main()
