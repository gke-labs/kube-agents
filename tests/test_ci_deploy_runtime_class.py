"""The smoke pipeline's Helm release pins the standard runtime, not the sandbox.

`charts/kube-agents` defaults `platformAgent.deployment.availability.runtimeClassName`
to `gvisor`, so a release that passes no override renders a sandboxed agent pod.
`hack/ci-deploy.sh` then reaches that pod over `kubectl port-forward`, which a
GKE Sandbox pod refuses -- the forward is established in the host-side CNI netns
while the listener lives in the sandbox's own network stack
(`scripts/exec_tunnel.py` is canonical). On a pool cluster with no `gvisor`
RuntimeClass registered the pod does not schedule at all and the rollout gate
times out instead.

Both failures are silent in the diff that causes them: nothing in `ci-deploy.sh`
mentions the sandbox, so the pipeline breaks the next time a chart default moves.
This pins the override, and pins that the chart still needs it -- drop the
`--set` and the first test fails; flip the chart back to an unsandboxed default
and the second one says so, at which point the override is dead weight rather
than load-bearing.
"""

import pathlib
import re
import shutil
import subprocess
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_CHART = _REPO_ROOT / "charts" / "kube-agents"

_RUNTIME_CLASS_KEY = "platformAgent.deployment.availability.runtimeClassName"


class CiDeployRuntimeClassTest(unittest.TestCase):
    def test_helm_release_pins_the_runtime_class_empty(self) -> None:
        text = _CI_DEPLOY.read_text()
        self.assertRegex(
            text,
            re.escape(f"--set \"{_RUNTIME_CLASS_KEY}=\""),
            f"hack/ci-deploy.sh must pass --set {_RUNTIME_CLASS_KEY}= to the "
            "chart: the chart's default is gvisor, and the job reaches the "
            "agent with kubectl port-forward.",
        )

    def test_the_chart_default_is_still_the_sandbox(self) -> None:
        values = (_CHART / "values.yaml").read_text()
        self.assertRegex(
            values,
            r"(?m)^\s*runtimeClassName: \"gvisor\"\s*$",
            "charts/kube-agents/values.yaml no longer defaults to the sandbox; "
            "the --set in hack/ci-deploy.sh is now redundant and should go.",
        )


class HelmRendersWithoutRuntimeClassTest(unittest.TestCase):
    """The override actually removes the field, rather than setting it to "".

    `compactFields` drops an all-unset `availability` block, so the empty value
    has to reach the template as empty rather than as the string "gvisor" that
    a mistyped key would leave in place. Only a real `helm` can show that, so
    this skips where the binary is absent (a contributor's laptop) and runs in
    CI, which installs one.
    """

    def test_rendered_cr_carries_no_runtime_class(self) -> None:
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
                "--set",
                f"{_RUNTIME_CLASS_KEY}=",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertNotIn("runtimeClassName", rendered)


if __name__ == "__main__":
    unittest.main()
