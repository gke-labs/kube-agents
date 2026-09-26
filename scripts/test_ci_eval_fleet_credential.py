"""The presubmit stops when the seeded fleet cannot be read as the reader.

`hack/ci-eval-pr.sh` sources `hack/fleet-kubeconfigs.sh` and calls
`write_fleet_kubeconfigs`. The runner exits `_FLEET_EXIT_READONLY_UNAVAILABLE`
when the read-only credential is unset or cannot be minted, and the call site
must turn that one code into a failed run -- every other non-zero exit stays a
warning, as before. This lifts the call site out of the script and runs it
under `set -euo pipefail` with the runner stubbed, so a rewording that drops
the branch, or a reshuffle that puts the warning back on the credential path,
fails here rather than on a pool project.
"""

import os
import pathlib
import re
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "hack" / "ci-eval-pr.sh"
RUNNER = REPO_ROOT / "hack" / "fleet-kubeconfigs.sh"
CALL_SITE = re.compile(r"^write_fleet_kubeconfigs \|\| \{\n.*?^\}$", re.S | re.M)


def call_site() -> str:
    match = CALL_SITE.search(SCRIPT.read_text(encoding="utf-8"))
    if match is None:  # pragma: no cover - a rewrite should say so loudly
        raise AssertionError(f"the write_fleet_kubeconfigs call site was not found in {SCRIPT}")
    return match.group(0)


def run_bash(script: str, **env: str) -> subprocess.CompletedProcess:
    """Run under an explicit environment: the developer's shell may export the
    very variables under test (the opt-in, the reader, Prow's JOB_NAME), and
    the lifted code must see only what the case sets."""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True, check=False,
        env={"PATH": os.environ["PATH"], **env},
    )


def run_call_site(runner_exit: int) -> subprocess.CompletedProcess:
    script = "\n".join(
        [
            "set -euo pipefail",
            f'source "{RUNNER}"',
            f"write_fleet_kubeconfigs() {{ return {runner_exit}; }}",
            "FLEET_READONLY_SA=reader@p.iam.gserviceaccount.com",
            "PROJECT_ID=p",
            call_site(),
            "echo reached-the-next-step",
        ]
    )
    return run_bash(script)


DEPLOY = REPO_ROOT / "hack" / "ci-deploy.sh"
PREFLIGHT = "preflight_fleet_reader"
PROW_REFUSAL = "_fleet_refuse_opt_in_under_prow || exit 1"
PROW_JOB = "pull-kube-agents-smoke-test"
# The gate's one external call, as a shell function that shadows the binary,
# so the pre-flight runs the real gate and the tests read its real message.
GCLOUD_DENIES = 'gcloud() { echo "ERROR: (gcloud.auth.print-access-token) PERMISSION_DENIED: Failed to impersonate" >&2; return 1; }'
GCLOUD_MINTS = "gcloud() { printf 'ya29.a-token'; }"
# Neither caller adds a repair of its own: the gate's message is the one a
# reader sees, and it already tells a job from a laptop.
REPAIR_PHRASES = ("pply bench/tf/fleet", "FLEET_ALLOW_RUNNER_CREDENTIAL")


def preflight_body() -> str:
    """The pre-flight function as written in ci-deploy.sh, lifted whole."""
    match = re.search(rf"^{PREFLIGHT}\(\) \{{\n.*?^\}}$", DEPLOY.read_text(encoding="utf-8"), re.S | re.M)
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"{PREFLIGHT}() not found in {DEPLOY}")
    return match.group(0)


def run_preflight(gcloud: str, **env: str) -> subprocess.CompletedProcess:
    """The real gate against a `gcloud` that mints or denies; whether the run
    is a Prow job is whatever `env` says."""
    script = "\n".join(
        [
            "set -euo pipefail",
            f'source "{RUNNER}"',
            gcloud,
            "PROJECT_ID=p",
            preflight_body(),
            PREFLIGHT,
            "echo reached-the-build",
        ]
    )
    return run_bash(script, **env)


class DeployPreflightTest(unittest.TestCase):
    """ci-deploy.sh runs the same gate before the build, so a project whose
    reader cannot be impersonated fails in seconds -- inside the dashboard's
    setup-death bound -- rather than after twenty minutes of build and deploy."""

    def test_an_unavailable_reader_stops_a_prow_deploy_with_the_pool_repair(self):
        done = run_preflight(GCLOUD_DENIES, JOB_NAME=PROW_JOB)
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertIn("cannot mint a read-only token as seeded-fleet-reader@p.iam.gserviceaccount.com", done.stderr)
        self.assertIn("re-apply bench/tf/fleet against p", done.stderr)
        self.assertIn("FATAL", done.stderr)
        self.assertNotIn("reached-the-build", done.stdout)

    def test_a_laptop_is_sent_to_the_opt_in_not_to_a_pool_repair(self):
        """Off Prow the mint fails because roles/owner cannot impersonate the
        reader, not because the project drifted; re-applying the fleet stack
        against a shared pool project is the one repair a laptop must not be
        told to make -- by the gate or by the caller, on the same stderr."""
        done = run_preflight(GCLOUD_DENIES)
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertIn("FLEET_ALLOW_RUNNER_CREDENTIAL=1", done.stderr)
        self.assertNotIn("pply bench/tf/fleet", done.stderr)
        self.assertIn("FATAL", done.stderr)
        self.assertNotIn("reached-the-build", done.stdout)

    def test_a_laptop_that_opted_in_passes_on_its_own_credential(self):
        done = run_preflight(GCLOUD_DENIES, FLEET_ALLOW_RUNNER_CREDENTIAL="1")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("reached-the-build", done.stdout)

    def test_a_prow_job_refuses_the_opt_in(self):
        """Set in a job's environment, the opt-in would restore the fallback
        this gate replaces for every leased run; the deploy stops before it
        chooses a reader, whatever the gate would have said."""
        done = run_preflight(GCLOUD_MINTS, JOB_NAME=PROW_JOB, FLEET_ALLOW_RUNNER_CREDENTIAL="1")
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertIn("FLEET_ALLOW_RUNNER_CREDENTIAL=1 is set in a Prow job", done.stderr)
        self.assertNotIn("reached-the-build", done.stdout)

    def test_a_mintable_reader_lets_the_deploy_continue(self):
        done = run_preflight(GCLOUD_MINTS, JOB_NAME=PROW_JOB)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("reached-the-build", done.stdout)

    def test_the_deploy_and_the_eval_default_the_same_reader(self):
        """One definition, `_fleet_reader_for_run`; both call sites use it,
        neither spells the account out, and both refuse the opt-in under Prow
        before choosing a reader."""
        for path in (DEPLOY, SCRIPT):
            text = path.read_text(encoding="utf-8")
            self.assertIn('_fleet_reader_for_run "${PROJECT_ID}"', text, path)
            self.assertNotIn("seeded-fleet-reader@${PROJECT_ID}", text, path)
            self.assertLess(text.index(PROW_REFUSAL), text.index('_fleet_reader_for_run "${PROJECT_ID}"'), path)

    def test_the_callers_leave_the_repair_to_the_gate(self):
        for lifted in (preflight_body(), call_site()):
            for phrase in REPAIR_PHRASES:
                self.assertNotIn(phrase, lifted)

    def test_the_developer_opt_in_leaves_the_reader_unset(self):
        """`FLEET_ALLOW_RUNNER_CREDENTIAL=1` on a developer's own project must
        reach the gate as no reader, or the gate cannot honour it."""
        def reader(**env: str) -> str:
            return run_bash(f'source "{RUNNER}"; _fleet_reader_for_run p', **env).stdout
        self.assertEqual("seeded-fleet-reader@p.iam.gserviceaccount.com", reader())
        self.assertEqual("", reader(FLEET_ALLOW_RUNNER_CREDENTIAL="1"))
        self.assertEqual("mine@p.iam.gserviceaccount.com", reader(FLEET_READONLY_SA="mine@p.iam.gserviceaccount.com", FLEET_ALLOW_RUNNER_CREDENTIAL="1"))

    def test_the_prow_refusal_fires_only_on_a_job_with_the_opt_in(self):
        def refusal(**env: str) -> int:
            return run_bash(f'source "{RUNNER}"; _fleet_refuse_opt_in_under_prow', **env).returncode
        self.assertEqual(0, refusal())
        self.assertEqual(0, refusal(FLEET_ALLOW_RUNNER_CREDENTIAL="1"))
        self.assertEqual(0, refusal(JOB_NAME="periodic-kube-agents-nightly"))
        self.assertEqual(1, refusal(JOB_NAME="periodic-kube-agents-nightly", FLEET_ALLOW_RUNNER_CREDENTIAL="1"))
        self.assertEqual(1, refusal(PULL_NUMBER="2005", FLEET_ALLOW_RUNNER_CREDENTIAL="1"))


class FleetCredentialCallSiteTest(unittest.TestCase):
    def test_an_unavailable_reader_fails_the_run(self):
        done = run_call_site(3)
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertIn("FATAL", done.stderr)
        self.assertNotIn("reached-the-next-step", done.stdout)

    def test_any_other_failure_is_still_a_warning(self):
        done = run_call_site(1)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("WARNING", done.stderr)
        self.assertIn("reached-the-next-step", done.stdout)

    def test_the_gate_code_is_the_runners_own(self):
        """The call site compares against the constant the runner exports, not
        a literal, so the two cannot drift apart."""
        self.assertIn("_FLEET_EXIT_READONLY_UNAVAILABLE", call_site())
        self.assertIn("_FLEET_EXIT_READONLY_UNAVAILABLE=3", RUNNER.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
