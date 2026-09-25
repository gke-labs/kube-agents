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


def run_preflight(gate_exit: int) -> subprocess.CompletedProcess:
    script = "\n".join(
        [
            "set -euo pipefail",
            f'source "{RUNNER}"',
            f"_fleet_require_readonly_credential() {{ return {gate_exit}; }}",
            "FLEET_READONLY_SA=reader@p.iam.gserviceaccount.com",
            "PROJECT_ID=p",
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
        self.assertNotIn("reached-the-build", done.stdout)

    def test_a_mintable_reader_lets_the_deploy_continue(self):
        done = run_preflight(0)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertIn("reached-the-build", done.stdout)

    def test_the_deploy_and_the_eval_default_the_same_reader(self):
        """One definition, `_fleet_default_reader`; both call sites use it."""
        for path in (DEPLOY, SCRIPT):
            self.assertIn('_fleet_default_reader "${PROJECT_ID}"', path.read_text(encoding="utf-8"), path)
        self.assertNotIn("seeded-fleet-reader@${PROJECT_ID}", SCRIPT.read_text(encoding="utf-8"))


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
