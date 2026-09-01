"""Tests for the platform-agent-gateway rollout budgets.

The companion to test_hindsight_probes.py, for the Deployment the redeploy
workflows actually gate on -- and for scripts/release/wait_for_gke_readiness.sh,
which waits on the same Deployments after the RC environment is provisioned and
is bound by the same rule. The same three numbers have to stay in the same
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
_READINESS_SCRIPT = _ROOT / "scripts" / "release" / "wait_for_gke_readiness.sh"

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


def _readiness_gate_seconds(deployment, variable):
    """The gate wait_for_gke_readiness.sh applies to one Deployment.

    Two halves, because the script gates through a shell constant rather than a
    literal: the constant's value, and the fact that this Deployment's `rollout
    status` is the line that reads it. Asserting only the value would keep
    passing if the two Deployments were pointed at the same constant again,
    which is the state this split exists to leave behind.
    """
    text = _READINESS_SCRIPT.read_text()

    declared = re.search(rf'readonly {re.escape(variable)}="(\d+)s"', text)
    assert declared, f"could not find readonly {variable} in {_READINESS_SCRIPT.name}"

    used = re.search(
        rf"kubectl rollout status deployment/{re.escape(deployment)}\b[^\n]*?"
        rf'--timeout="\$\{{{re.escape(variable)}\}}"',
        text,
    )
    assert used, f"{deployment}'s rollout status in {_READINESS_SCRIPT.name} does not use {variable}"

    return int(declared.group(1))


class ReleaseReadinessGateTest(unittest.TestCase):
    """The RC pipeline's own gates, which are not the deploy workflows'.

    wait_for_gke_readiness.sh waits on the same two Deployments after the RC
    environment is provisioned, so it is bound by the same ordering -- but it
    sat outside this file's scope and ran a single 300s gate for both. That was
    under the gateway's 605s startupProbe budget, and went unnoticed while the
    RC provisioned Standard clusters: a fresh Autopilot cluster pays node
    scale-up and a first image pull before the container starts.
    """

    def setUp(self):
        self.startup = _gateway_startup_budget_seconds()
        self.deadline = _gateway_progress_deadline_seconds()
        self.gateway_gate = _readiness_gate_seconds(
            "platform-agent-gateway", "GATEWAY_READINESS_TIMEOUT"
        )
        self.litellm_gate = _readiness_gate_seconds("litellm", "LITELLM_READINESS_TIMEOUT")

    def test_the_gateway_gate_covers_the_startup_budget_and_the_image_pull(self):
        self.assertGreaterEqual(
            self.gateway_gate,
            self.startup + _PULL_ALLOWANCE_SECONDS,
            f"a {self.gateway_gate}s gate leaves "
            f"{self.gateway_gate - self.startup}s for node scale-up and an image pull on "
            f"top of a {self.startup}s startupProbe budget; on a fresh Autopilot cluster "
            "this reds an RC that was still coming up",
        )

    def test_the_gateway_gate_stays_under_the_progress_deadline(self):
        self.assertLess(
            self.gateway_gate,
            self.deadline,
            f"a {self.gateway_gate}s gate against a {self.deadline}s progressDeadlineSeconds "
            "cannot run its full length",
        )

    def test_the_litellm_gate_stays_under_the_default_progress_deadline(self):
        # litellm sets no progressDeadlineSeconds of its own, so unlike the
        # gateway it has the 600s default as its ceiling, not 1200s.
        self.assertLess(
            self.litellm_gate,
            _DEFAULT_PROGRESS_DEADLINE_SECONDS,
            f"litellm runs on the {_DEFAULT_PROGRESS_DEADLINE_SECONDS}s default deadline; a "
            f"{self.litellm_gate}s gate cannot run its full length. Set an explicit deadline "
            "first, as the gateway does",
        )

    def test_the_two_deployments_do_not_share_one_gate(self):
        """The ceilings differ by 600s, so one number cannot respect both."""
        self.assertNotEqual(
            self.gateway_gate,
            self.litellm_gate,
            "a single readiness gate for both Deployments is either under the gateway's "
            "cold-start cost or over litellm's default progress deadline",
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
