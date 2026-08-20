"""Tests for the hindsight-api startup contract (#712).

    python3 -m unittest discover -s tests -p 'test_*.py'

Stdlib unittest, no pytest, matching the other suites in this directory.

`hindsight-api` is the slowest-starting workload the install deploys: two
transformer models are baked into a 1.4 GB image and loaded in-process before
the API binds :8888. Nothing about that is visible in a manifest review, and
the failure it causes is not visible in CI either — it needs a node that has
never pulled the image. Before #712 the deployment had no `startupProbe` and a
`livenessProbe` with `initialDelaySeconds: 30`, so the third failure landed at
t=50s and the kubelet killed the container mid-load; each kill restarts the
load from nothing, and the install's rollout gate is what eventually fails.

Three budgets have to stay ordered for a cold roll to survive, and they live in
two files in two languages:

    startupProbe budget  <  rollout gate            <  progressDeadlineSeconds
    (api.yaml)              (helm_release timeout,     (api.yaml)
                             full-install main.tf)

Below the first, the kubelet kills a pod that is loading normally. Above the
second, the install gives up on one. Above the third, the Deployment reports
"exceeded its progress deadline" and helm's wait stops early whatever timeout
it was given, so a gate raised past it buys nothing. Nothing in YAML or HCL
keeps the three in that order, and each one alone looks reasonable — which is
what these tests are for. The rationale for the individual numbers belongs to
`api.yaml`'s probe comment, not here.

The gate is the `helm_release.kube_agents` wait in the full-install
composition, whose timeout these tests read from main.tf.
"""

import pathlib
import re
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_API_YAML = _ROOT / "k8s-operator" / "config" / "integrations" / "hindsight" / "api.yaml"
_FULL_INSTALL_MAIN_TF = _ROOT / "terraform" / "examples" / "full-install" / "main.tf"

# What the gate must have over the startupProbe budget, in seconds, for the
# pull that precedes the container starting at all. The pinned image is 1.4 GB
# compressed; four minutes is a slow-registry pull, not a typical one.
_PULL_ALLOWANCE_SECONDS = 240


def _api_deployment():
    """The hindsight-api Deployment, as committed.

    api.yaml is a multi-document file whose image is an unexpanded
    `${HINDSIGHT_API_IMAGE}`; safe_load_all reads it happily, since the
    substitution the deploy target does is not YAML-significant.
    """
    docs = [d for d in yaml.safe_load_all(_API_YAML.read_text()) if d]
    deployments = [
        d
        for d in docs
        if d.get("kind") == "Deployment" and d["metadata"]["name"] == "hindsight-api"
    ]
    assert len(deployments) == 1, f"expected one hindsight-api Deployment, got {len(deployments)}"
    return deployments[0]


def _api_container():
    containers = _api_deployment()["spec"]["template"]["spec"]["containers"]
    by_name = {c["name"]: c for c in containers}
    assert "api" in by_name, f"no container named 'api' in {sorted(by_name)}"
    return by_name["api"]


def _budget_seconds(probe):
    """How long a probe tolerates failure before the kubelet acts.

    initialDelaySeconds is counted rather than assumed absent: a probe that
    grows one later would otherwise under-report its budget to the ordering
    assertions below, which is the direction that hides a problem.
    """
    return probe.get("initialDelaySeconds", 0) + probe.get("periodSeconds", 10) * probe.get(
        "failureThreshold", 3
    )


def _kube_agents_release_block():
    """The helm_release.kube_agents block from the full-install composition."""
    text = _FULL_INSTALL_MAIN_TF.read_text()
    match = re.search(r'resource "helm_release" "kube_agents" \{.*?\n\}\n', text, re.DOTALL)
    assert match, "could not find helm_release.kube_agents in full-install main.tf"
    return match.group(0)


def _default_gate_seconds():
    """The rollout budget the install waits for: the helm_release timeout.

    Read rather than hardcoded so raising the timeout cannot leave this suite
    asserting against a number no install uses. The helm provider's timeout is
    plain seconds, no unit suffix.
    """
    match = re.search(r"^\s*timeout\s*=\s*(\d+)\s*$", _kube_agents_release_block(), re.MULTILINE)
    assert match, "could not find the timeout on helm_release.kube_agents"
    return int(match.group(1))


class StartupProbeTest(unittest.TestCase):
    def setUp(self):
        self.container = _api_container()

    def test_a_startup_probe_exists(self):
        self.assertIn(
            "startupProbe",
            self.container,
            "hindsight-api loads its models before binding :8888; without a "
            "startupProbe the liveness probe kills cold containers mid-load (#712)",
        )

    def test_the_startup_probe_hits_the_health_endpoint_on_the_container_port(self):
        get = self.container["startupProbe"]["httpGet"]
        self.assertEqual(get["path"], "/health")
        self.assertEqual(get["port"], 8888)

    def test_the_startup_budget_covers_a_cold_model_load(self):
        # Five minutes. A warm-image start measured 49s on an e2-standard-4;
        # the margin is for a cold page cache and a node under CPU pressure
        # from a co-scheduled pod, where too tight a budget resumes the
        # original crash loop.
        self.assertGreaterEqual(_budget_seconds(self.container["startupProbe"]), 300)

    def test_liveness_and_readiness_still_exist(self):
        # The startupProbe suspends them; it does not replace them. A wedged
        # API that has once served /health must still be restarted.
        for name in ("livenessProbe", "readinessProbe"):
            with self.subTest(probe=name):
                get = self.container[name]["httpGet"]
                self.assertEqual(get["path"], "/health")
                self.assertEqual(get["port"], 8888)

    def test_liveness_and_readiness_carry_no_initial_delay(self):
        # Dead configuration with a startupProbe present, and misleading: it
        # is the shape the pre-#712 manifest used to express a start budget,
        # and reintroducing it is how the intent gets lost again.
        for name in ("livenessProbe", "readinessProbe"):
            with self.subTest(probe=name):
                self.assertNotIn("initialDelaySeconds", self.container[name])


class RolloutBudgetTest(unittest.TestCase):
    """The three budgets, and the order they have to stay in."""

    def setUp(self):
        self.startup = _budget_seconds(_api_container()["startupProbe"])
        self.gate = _default_gate_seconds()
        self.deadline = _api_deployment()["spec"].get("progressDeadlineSeconds")

    def test_the_gate_covers_the_startup_budget_and_the_image_pull(self):
        self.assertGreaterEqual(
            self.gate,
            self.startup + _PULL_ALLOWANCE_SECONDS,
            f"a {self.gate}s gate leaves {self.gate - self.startup}s for a 1.4 GB pull "
            f"on top of a {self.startup}s startupProbe budget; the terraform apply "
            "errors when it expires, so this fails the install on a slow node "
            "rather than tolerating it",
        )

    def test_the_progress_deadline_outlasts_the_gate(self):
        # Without this the gate is decorative above 600s: helm's wait returns
        # "exceeded its progress deadline" the moment the Deployment gives up,
        # however long the caller asked to wait.
        self.assertIsNotNone(
            self.deadline,
            "hindsight-api needs an explicit progressDeadlineSeconds; the 600s default "
            "silently caps how long the install can wait for a cold roll",
        )
        self.assertGreater(self.deadline, self.gate)

    def test_the_release_waits_with_an_explicit_gate(self):
        # wait defaulted-on is not enough: the provider's default timeout is
        # 300s, below the startup budget plus a slow pull, so leaving the
        # attribute implicit is how the gate and the probe budget drift apart.
        block = _kube_agents_release_block()
        self.assertRegex(
            block,
            r"(?m)^\s*wait\s*=\s*true\s*$",
            "helm_release.kube_agents must wait explicitly; the wait is the "
            "install's only rollout gate",
        )
        self.assertRegex(
            block,
            r"(?m)^\s*timeout\s*=\s*\d+\s*$",
            "helm_release.kube_agents needs an explicit timeout; the provider "
            "default (300s) gives up on a cold hindsight-api roll that is "
            "loading normally",
        )


if __name__ == "__main__":
    unittest.main()
