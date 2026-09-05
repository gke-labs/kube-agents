"""The smoke pipeline generates the shell sandbox keypair before installing.

The chart cannot generate this one -- sprig emits PEM and has no encoder for
`authorized_keys` form -- so `charts/kube-agents/templates/
platform-agent-shell-authorized-keys.yaml` renders the Secret only from a
`SANDBOX_SSH_PUBLIC_KEY` the caller supplies. `install.sh` supplies it through
the Terraform composition, `upgrade.sh` through `backfill_sandbox_ssh_key`, and
`hack/ci-deploy.sh` by generating a throwaway pair per run.

A release that supplies none fails in the least legible way available: the
Secret is absent, the sandbox pod's mount of it is not optional, and kubelet
leaves the pod in `ContainerCreating` reporting the cause only in a pod event
until the rollout gate times out fifteen minutes later. That is what happened on
#913 before this test existed, so both halves are pinned here -- the generation
and the two `--set-file`s that carry it -- and the chart side is pinned too, so
that a chart that learns to generate its own pair shows up as this test failing
rather than as dead code in the pipeline.
"""

import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_AUTHORIZED_KEYS_TEMPLATE = (
    _REPO_ROOT
    / "charts"
    / "kube-agents"
    / "templates"
    / "platform-agent-shell-authorized-keys.yaml"
)

_PRIVATE_KEY_VALUE = "platformAgent.credentials.data.SANDBOX_SSH_PRIVATE_KEY"
_PUBLIC_KEY_VALUE = "platformAgent.credentials.data.SANDBOX_SSH_PUBLIC_KEY"


class CiDeployGeneratesTheSandboxKeypairTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _CI_DEPLOY.read_text()

    def test_a_keypair_is_generated(self) -> None:
        self.assertRegex(
            self.text,
            r"(?m)^ssh-keygen ",
            "hack/ci-deploy.sh must generate an SSH keypair: with no public "
            "half the chart renders no authorized-keys Secret and the sandbox "
            "pod never leaves ContainerCreating.",
        )

    def test_both_halves_reach_the_release(self) -> None:
        for value in (_PRIVATE_KEY_VALUE, _PUBLIC_KEY_VALUE):
            with self.subTest(value=value):
                self.assertRegex(
                    self.text,
                    re.escape(f'--set-file "{value}='),
                    f"hack/ci-deploy.sh must pass {value} to the chart. "
                    "--set-file rather than --set-string: the private half is "
                    "a PEM, and Helm's --set parser reads its newlines and "
                    "commas as syntax.",
                )

    def test_the_generation_precedes_the_release(self) -> None:
        keygen = self.text.index("ssh-keygen ")
        release = self.text.index("helm upgrade --install")
        self.assertLess(
            keygen,
            release,
            "the keypair must exist before the release that reads it.",
        )

    def test_ssh_keygen_is_checked_for_up_front(self) -> None:
        """A missing binary must fail at second zero, not at minute fifteen.

        Without the precheck the script runs to the rollout gate and reports a
        pod stuck in ContainerCreating -- a symptom four steps removed from the
        cause, and the reason this failure cost a whole eval run to diagnose.
        """
        precheck = self.text.index("command -v ssh-keygen")
        keygen = self.text.index("ssh-keygen -q")
        self.assertLess(precheck, keygen)

    def test_the_chart_still_needs_the_key_supplied(self) -> None:
        template = _AUTHORIZED_KEYS_TEMPLATE.read_text()
        self.assertIn(
            "SANDBOX_SSH_PUBLIC_KEY",
            template,
            "the chart no longer reads a supplied public key; the generation "
            "in hack/ci-deploy.sh is now redundant and should go.",
        )


if __name__ == "__main__":
    unittest.main()
