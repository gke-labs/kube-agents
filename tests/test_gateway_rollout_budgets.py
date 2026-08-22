"""Tests for the platform-agent-gateway rollout budgets.

The companion to test_hindsight_probes.py, for the Deployment the redeploy
workflows actually gate on. The same three numbers have to stay in the same
order:

    startupProbe budget  <  rollout gate  <  progressDeadlineSeconds

The upper bound is the hard one. Past the deadline the Deployment reports
ProgressDeadlineExceeded and any caller's wait returns early however long it
was given, so a gate at or above the deadline buys nothing.

The gateway spent a long time violating this on both sides. Kubernetes
defaults progressDeadlineSeconds to 600s, and nothing set it, while
agentAPIProbe(10, 60) sanctions a 605s cold boot -- the kubelet was told to
tolerate a boot the Deployment gives up on. The rollout gate sat at 180s,
under both, and reported red on deploys that had succeeded: a gateway pod
measured 215s to Ready in autopush and 259s in staging.

Every number is read from the source that owns it rather than hardcoded here,
so raising one cannot leave this suite asserting against a value no install
uses.
"""

import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MANIFESTS_GO = _ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
_AGENT_WORKFLOW = _ROOT / ".github" / "workflows" / "reusable-deploy-agent.yml"
_INTEGRATIONS_WORKFLOW = _ROOT / ".github" / "workflows" / "reusable-deploy-integrations.yml"

# What the gate must have over the startupProbe budget, in seconds, for the
# node scale-up and image pull that precede the container starting at all.
# Matches the allowance test_hindsight_probes.py derives its gate from.
_PULL_ALLOWANCE_SECONDS = 240

# Kubernetes' default when a Deployment does not set progressDeadlineSeconds.
# The integrations Deployments below rely on it rather than setting their own.
_DEFAULT_PROGRESS_DEADLINE_SECONDS = 600


def _gateway_startup_budget_seconds():
    """How long the gateway's startupProbe tolerates failure.

    Both halves come from the Go source: the call site fixes periodSeconds and
    failureThreshold, and agentAPIProbe fixes initialDelaySeconds. Counting the
    initial delay matters -- omitting it under-reports the budget, which is the
    direction that hides a violation.
    """
    text = _MANIFESTS_GO.read_text()

    call = re.search(r"StartupProbe:\s*agentAPIProbe\((\d+),\s*(\d+)\)", text)
    assert call, "could not find the gateway StartupProbe call to agentAPIProbe"
    period, failure_threshold = int(call.group(1)), int(call.group(2))

    body = re.search(r"func agentAPIProbe\([^)]*\)[^{]*\{.*?\n\}", text, re.DOTALL)
    assert body, "could not find func agentAPIProbe"
    initial = re.search(r"InitialDelaySeconds:\s*(\d+)", body.group(0))
    assert initial, "could not find InitialDelaySeconds in agentAPIProbe"

    return int(initial.group(1)) + period * failure_threshold


def _gateway_progress_deadline_seconds():
    """The ceiling the operator pins on the gateway Deployment."""
    match = re.search(
        r"const gatewayProgressDeadlineSeconds int32 = (\d+)", _MANIFESTS_GO.read_text()
    )
    assert match, "could not find gatewayProgressDeadlineSeconds in platformagent_manifests.go"
    return int(match.group(1))


def _rollout_gate_seconds(workflow, deployment):
    """The --timeout on a `kubectl rollout status` for one Deployment."""
    match = re.search(
        rf"kubectl rollout status deployment/{re.escape(deployment)}\b[^\n]*?--timeout=(\d+)s",
        workflow.read_text(),
    )
    assert match, f"could not find the rollout gate for {deployment} in {workflow.name}"
    return int(match.group(1))


class GatewayRolloutBudgetTest(unittest.TestCase):
    """The three gateway budgets, and the order they have to stay in."""

    def setUp(self):
        self.startup = _gateway_startup_budget_seconds()
        self.gate = _rollout_gate_seconds(_AGENT_WORKFLOW, "platform-agent-gateway")
        self.deadline = _gateway_progress_deadline_seconds()

    def test_the_gate_covers_the_startup_budget_and_the_image_pull(self):
        self.assertGreaterEqual(
            self.gate,
            self.startup + _PULL_ALLOWANCE_SECONDS,
            f"a {self.gate}s gate leaves {self.gate - self.startup}s for node scale-up and "
            f"an image pull on top of a {self.startup}s startupProbe budget; the workflow "
            "fails the deploy when it expires, so this reds a rollout that succeeded",
        )

    def test_the_progress_deadline_outlasts_the_gate(self):
        # Without this the gate is decorative: kubectl rollout status returns
        # "exceeded its progress deadline" the moment the Deployment gives up,
        # however long the caller asked to wait.
        self.assertGreater(
            self.deadline,
            self.gate,
            f"a {self.gate}s gate against a {self.deadline}s progressDeadlineSeconds cannot "
            "run its full length; raise the deadline in the operator, not just the gate",
        )

    def test_the_progress_deadline_outlasts_the_startup_budget(self):
        # The inversion this file exists for: the kubelet tolerating a longer
        # cold boot than the Deployment will wait for means a pod using its
        # sanctioned startup time is failed by the Deployment regardless of
        # what any gate says.
        self.assertGreater(
            self.deadline,
            self.startup,
            f"agentAPIProbe sanctions a {self.startup}s cold boot the Deployment abandons "
            f"at {self.deadline}s",
        )


class IntegrationsRolloutGateTest(unittest.TestCase):
    """The integrations gates rely on the default deadline, so they stay under it."""

    def test_gates_stay_under_the_default_progress_deadline(self):
        for deployment in ("litellm", "github-token-minter"):
            with self.subTest(deployment=deployment):
                gate = _rollout_gate_seconds(_INTEGRATIONS_WORKFLOW, deployment)
                self.assertLess(
                    gate,
                    _DEFAULT_PROGRESS_DEADLINE_SECONDS,
                    f"{deployment} sets no progressDeadlineSeconds, so it runs on the "
                    f"{_DEFAULT_PROGRESS_DEADLINE_SECONDS}s default; a {gate}s gate cannot "
                    "run its full length. Set an explicit deadline first, as the gateway does",
                )


if __name__ == "__main__":
    unittest.main()
