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
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


DEPLOY = REPO_ROOT / "hack" / "ci-deploy.sh"
PREFLIGHT = "preflight_fleet_reader"


def preflight_body() -> str:
    """The pre-flight function as written in ci-deploy.sh, lifted whole."""
    match = re.search(rf"^{PREFLIGHT}\(\) \{{\n.*?^\}}$", DEPLOY.read_text(encoding="utf-8"), re.S | re.M)
    if match is None:  # pragma: no cover - a rename should say so loudly
        raise AssertionError(f"{PREFLIGHT}() not found in {DEPLOY}")
    return match.group(0)


def run_preflight(gate_exit: int, prow: bool = True) -> subprocess.CompletedProcess:
    script = "\n".join(
        [
            "set -euo pipefail",
            f'source "{RUNNER}"',
            f"_fleet_require_readonly_credential() {{ return {gate_exit}; }}",
            "FLEET_READONLY_SA=reader@p.iam.gserviceaccount.com",
            "PROJECT_ID=p",
            f"IS_PROW_RUN={'true' if prow else 'false'}",
            preflight_body(),
            PREFLIGHT,
            "echo reached-the-build",
        ]
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=False)


class DeployPreflightTest(unittest.TestCase):
    """ci-deploy.sh runs the same gate before the build, so a project whose
    reader cannot be impersonated fails in seconds -- inside the dashboard's
    setup-death bound -- rather than after twenty minutes of build and deploy."""

    def test_an_unavailable_reader_stops_the_deploy(self):
        done = run_preflight(3)
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertIn("FATAL", done.stderr)
        self.assertIn("reader@p.iam.gserviceaccount.com", done.stderr)
        self.assertIn("Re-apply bench/tf/fleet against p", done.stderr)
        self.assertNotIn("reached-the-build", done.stdout)

    def test_a_laptop_is_sent_to_the_opt_in_not_to_a_pool_repair(self):
        """Off Prow the mint fails because roles/owner cannot impersonate the
        reader, not because the project drifted; re-applying the fleet stack
        against a shared pool project is the one repair a laptop must not be
        told to make."""
        done = run_preflight(3, prow=False)
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertIn("FATAL", done.stderr)
        self.assertIn("FLEET_ALLOW_RUNNER_CREDENTIAL=1", done.stderr)
        self.assertNotIn("Re-apply", done.stderr)
        self.assertNotIn("reached-the-build", done.stdout)

    def test_a_mintable_reader_lets_the_deploy_continue(self):
        done = run_preflight(0)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("reached-the-build", done.stdout)

    def test_the_deploy_and_the_eval_default_the_same_reader(self):
        """One definition, `_fleet_reader_for_run`; both call sites use it,
        and neither spells the account out."""
        for path in (DEPLOY, SCRIPT):
            text = path.read_text(encoding="utf-8")
            self.assertIn('_fleet_reader_for_run "${PROJECT_ID}"', text, path)
            self.assertNotIn("seeded-fleet-reader@${PROJECT_ID}", text, path)

    def test_the_developer_opt_in_leaves_the_reader_unset(self):
        """`FLEET_ALLOW_RUNNER_CREDENTIAL=1` on a developer's own project must
        reach the gate as no reader, or the gate cannot honour it."""
        def reader(**env: str) -> str:
            # An explicit environment: the developer's shell may export the
            # very variables under test, and the helper must not see them.
            done = subprocess.run(
                ["bash", "-c", f'source "{RUNNER}"; _fleet_reader_for_run p'],
                capture_output=True, text=True, check=False,
                env={"PATH": os.environ["PATH"], **env},
            )
            return done.stdout
        self.assertEqual("seeded-fleet-reader@p.iam.gserviceaccount.com", reader())
        self.assertEqual("", reader(FLEET_ALLOW_RUNNER_CREDENTIAL="1"))
        self.assertEqual("mine@p.iam.gserviceaccount.com", reader(FLEET_READONLY_SA="mine@p.iam.gserviceaccount.com", FLEET_ALLOW_RUNNER_CREDENTIAL="1"))


class FleetCredentialCallSiteTest(unittest.TestCase):
    def test_an_unavailable_reader_fails_the_run(self):
        done = run_call_site(3)
        self.assertEqual(1, done.returncode, done.stderr)
        self.assertIn("FATAL", done.stderr)
        self.assertIn("reader@p.iam.gserviceaccount.com", done.stderr)
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
