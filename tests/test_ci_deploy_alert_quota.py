"""The smoke pipeline's Helm release turns the Warning-alert daily cap off.

`session_kv_server.py` caps Warning alerts at 5 per UTC day, fleet-wide per
install (`ALERT_DAILY_LIMIT_WARNING`, #641). On an eval install that cap
suppresses the alerts the bench scenarios exist to generate: every smoke build
leasing the same pool project that day spends the shared budget, and once it
is gone the `autoops-warning-event-triage` plant waits 300s for an alert the
daemon has already dropped (#1101). `hack/ci-deploy.sh` therefore sets the
variable to `0` — the daemon's documented off-switch, pinned uncapped by
`test_zero_limit_never_suppresses` in
`agents/platform/scripts/test_session_kv_server.py` — without touching the
production default.

The value rides three hops to reach the daemon, and each can break silently:
the `--set-string` in `ci-deploy.sh` (dropping it re-reds the eval, but only
on the days the shared budget happens to run out), the chart template that
renders `platformAgent.deployment.env` onto the PlatformAgent CR, and the
operator's sandbox env allowlist, which drops any `spec.deployment.env` entry
it does not recognise rather than erroring. One test per hop.
"""

import pathlib
import shutil
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_OPERATOR_MANIFESTS = (
    _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
)

_ENV_NAME_FLAG = '--set-string "platformAgent.deployment.env[0].name=ALERT_DAILY_LIMIT_WARNING"'
_ENV_VALUE_FLAG = (
    '--set-string "platformAgent.deployment.env[0].value=${EVAL_ALERT_DAILY_LIMIT_WARNING}"'
)
_OFF_SWITCH = 'readonly EVAL_ALERT_DAILY_LIMIT_WARNING="0"'


class CiDeployAlertQuotaTest(unittest.TestCase):
    def test_helm_release_uncaps_warning_alerts(self) -> None:
        text = _CI_DEPLOY.read_text()
        for needle in (_ENV_NAME_FLAG, _ENV_VALUE_FLAG, _OFF_SWITCH):
            self.assertIn(
                needle,
                text,
                "hack/ci-deploy.sh must pass ALERT_DAILY_LIMIT_WARNING=0 to the "
                "chart: the daemon's 5/day fleet-wide Warning cap otherwise "
                "suppresses the alerts bench scenarios plant (#1101), and the "
                "failure is intermittent, not immediate.",
            )

    def test_the_operator_allowlist_carries_the_variable(self) -> None:
        # The operator copies spec.deployment.env into the container only for
        # allowlisted names and silently drops the rest, so removing this entry
        # would leave the --set in ci-deploy.sh rendering onto the CR and
        # reaching nothing.
        text = _OPERATOR_MANIFESTS.read_text()
        self.assertIn(
            '"ALERT_DAILY_LIMIT_WARNING":',
            text,
            "safeSandboxEnvOverrides no longer allowlists "
            "ALERT_DAILY_LIMIT_WARNING; the eval deploy's override in "
            "hack/ci-deploy.sh is silently dropped without it.",
        )


class HelmRendersTheOverrideTest(unittest.TestCase):
    """The --set pair actually lands on the rendered PlatformAgent CR.

    A mistyped values key would render a CR with no env block at all rather
    than fail, so only a real `helm template` can show the hop works. Skips
    where the binary is absent (a contributor's laptop) and runs in CI, which
    installs one.
    """

    def test_rendered_cr_carries_the_env_var(self) -> None:
        if shutil.which("helm") is None:
            self.skipTest("helm not installed")
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "t",
                str(_CHART),
                "--set-string",
                "platformAgent.harness.clusterName=c",
                "--set-string",
                "platformAgent.harness.location=us-central1",
                "--set-string",
                "platformAgent.harness.projectId=p",
                "--set-string",
                "platformAgent.deployment.env[0].name=ALERT_DAILY_LIMIT_WARNING",
                "--set-string",
                "platformAgent.deployment.env[0].value=0",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn('name: "ALERT_DAILY_LIMIT_WARNING"', rendered)
        self.assertIn('value: "0"', rendered)


if __name__ == "__main__":
    unittest.main()
