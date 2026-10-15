"""The smoke pipeline's Helm release turns the alert daily caps off.

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

`ALERT_DAILY_LIMIT_DRIFT` is the same cap over the drift bucket and is off for
a sharper reason than a shared budget. A drift case spends one inject per audit
record that survives the classifier, and the regression it exists to catch — a
classifier that stops filtering — spends one per record that should have been
dropped, so the failing run is the one that needs the most headroom. Under any
finite cap, a repetition that begins with one slot left files a card for the
first record and is refused the rest, which is indistinguishable from a working
filter: the case greens on the bug it was written to find.

Each value rides three hops to reach the daemon, and each can break silently:
the `--set-string` in `ci-deploy.sh` (dropping it re-reds the eval, but only
on the days the shared budget happens to run out), the chart template that
renders `platformAgent.deployment.env` onto the PlatformAgent CR, and the
operator's sandbox env allowlist, which drops any `spec.deployment.env` entry
it does not recognise rather than erroring. One test per hop.

The detector's own enablement and the subscription it pulls are a separate
concern and live in `test_ci_deploy_drift_detector.py`.
"""

import pathlib
import re
import shutil
import subprocess
import unittest

import yaml

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

_DRIFT_NAME_FLAG = '--set-string "platformAgent.deployment.env[1].name=ALERT_DAILY_LIMIT_DRIFT"'
_DRIFT_VALUE_FLAG = (
    '--set-string "platformAgent.deployment.env[1].value=${EVAL_ALERT_DAILY_LIMIT_DRIFT}"'
)
_DRIFT_OFF_SWITCH = 'readonly EVAL_ALERT_DAILY_LIMIT_DRIFT="0"'


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

    def test_helm_release_uncaps_drift_alerts(self) -> None:
        text = _CI_DEPLOY.read_text()
        for needle in (_DRIFT_NAME_FLAG, _DRIFT_VALUE_FLAG, _DRIFT_OFF_SWITCH):
            self.assertIn(
                needle,
                text,
                "hack/ci-deploy.sh must pass ALERT_DAILY_LIMIT_DRIFT=0 to the "
                "chart: under a finite cap a drift case can green on the very "
                "regression it was written to catch, because a refused inject "
                "and a correctly filtered record look identical from the board.",
            )

    def test_no_two_env_overrides_share_an_index(self) -> None:
        # helm's --set array indices are positional and silent about a
        # collision: an override added at an index another already uses
        # overwrites it rather than appending, and the rendered CR carries one
        # variable where the deploy passed two -- no error, no warning, and
        # whichever cap lost is back at its production default. Derived from
        # the file rather than listed here so the next variable added to the
        # deploy is covered without this test being edited, which is the edit
        # that would be forgotten.
        text = _CI_DEPLOY.read_text()
        names = re.findall(r"platformAgent\.deployment\.env\[(\d+)\]\.name=(\w+)", text)
        values = re.findall(r"platformAgent\.deployment\.env\[(\d+)\]\.value=", text)
        self.assertEqual(
            len(names),
            len({index for index, _ in names}),
            f"two platformAgent.deployment.env overrides in hack/ci-deploy.sh share "
            f"an index: {names}. The later one wins and the other variable never "
            "reaches the container.",
        )
        self.assertEqual(
            sorted(index for index, _ in names),
            sorted(values),
            "every platformAgent.deployment.env[N].name in hack/ci-deploy.sh needs "
            "its .value at the same N; a name with no value renders an entry the "
            "CRD rejects.",
        )
        self.assertEqual(
            sorted(int(index) for index, _ in names),
            list(range(len(names))),
            f"the platformAgent.deployment.env indices in hack/ci-deploy.sh are not "
            f"contiguous from 0: {names}. helm fills a skipped index with null, "
            "which renders a null entry into spec.deployment.env.",
        )

    def test_the_operator_allowlist_carries_the_variables(self) -> None:
        # The operator copies spec.deployment.env into the container only for
        # allowlisted names and silently drops the rest, so removing an entry
        # would leave the --set in ci-deploy.sh rendering onto the CR and
        # reaching nothing.
        text = _OPERATOR_MANIFESTS.read_text()
        for name in ("ALERT_DAILY_LIMIT_WARNING", "ALERT_DAILY_LIMIT_DRIFT"):
            self.assertIn(
                f'"{name}":',
                text,
                f"safeSandboxEnvOverrides no longer allowlists {name}; the eval "
                "deploy's override in hack/ci-deploy.sh is silently dropped "
                "without it.",
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
                "--set-string",
                "platformAgent.deployment.env[1].name=ALERT_DAILY_LIMIT_DRIFT",
                "--set-string",
                "platformAgent.deployment.env[1].value=0",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        # Parsed rather than grepped: both entries render the same `value: "0"`
        # line, so a substring check cannot tell two overrides that landed from
        # one that landed twice, which is the failure the positional --set
        # indices make possible.
        crs = [
            doc for doc in yaml.safe_load_all(rendered)
            if doc and doc.get("kind") == "PlatformAgent"
        ]
        self.assertEqual(len(crs), 1, "expected exactly one PlatformAgent in the render")
        env = crs[0]["spec"]["deployment"]["env"]
        self.assertEqual(
            {entry["name"]: entry["value"] for entry in env},
            {"ALERT_DAILY_LIMIT_WARNING": "0", "ALERT_DAILY_LIMIT_DRIFT": "0"},
        )


if __name__ == "__main__":
    unittest.main()
