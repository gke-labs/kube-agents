"""Tests that the kustomize platform Service agrees with the one the operator builds.

`deploy/kustomize/platform/service.yaml` and `buildPlatformService` in
`platformagent_manifests.go` describe the same object: the operator applies its
version with server-side apply and ForceOwnership, and the overlay ships the
same Service so a reader can see what an install exposes. Nothing kept the two
in step, and they drifted -- the manifest published `targetPort: 8642` while the
operator targeted 8643, and selected `app: platform-agent` where the operator
labels its gateway pods `<agent-name>-gateway`. Both defects survived because
each file is correct on its own terms and no test compares them.

The two values that drifted are each read from the source that owns them, so
changing the operator's `api` port or its gateway-pod label without changing the
manifest fails here rather than in a cluster. Two things are deliberately not
compared: the `dashboard` port, where the operator uses a named port and the
manifest a number, and the `replicas > 1` selector narrowing to the leader. Both
would need the test to model the operator rather than read it.
"""

import pathlib
import re
import unittest

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SERVICE_YAML = _ROOT / "deploy" / "kustomize" / "platform" / "service.yaml"
_MANIFESTS_GO = _ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"

# The agent name this manifest is written for: the Service's own metadata.name, and the
# CR name the operator derives the gateway pod label from. The NetworkPolicies beside it
# select `app.kubernetes.io/name`, a constant, so the Service is the only object in the
# overlay this name binds -- and it can only be correct for one.
_AGENT_NAME = "platform-agent"
_GATEWAY_LABEL_SUFFIX = "-gateway"

# The `api` ServicePort inside buildPlatformService, whose body is the only place
# the published port and the container port it targets are stated together.
_API_PORT_BLOCK = re.compile(
    r'Name:\s+"api",\s*\n\s*Port:\s+(?P<port>\d+),\s*\n\s*TargetPort:\s+intstr\.FromInt32\((?P<target>\d+)\)'
)


def _service_manifest():
    return yaml.safe_load(_SERVICE_YAML.read_text())


def _api_port_from_operator():
    match = _API_PORT_BLOCK.search(_MANIFESTS_GO.read_text())
    if match is None:
        raise AssertionError(
            f"no `api` ServicePort with an intstr.FromInt32 target found in {_MANIFESTS_GO}; "
            "buildPlatformService has been rewritten and this test has to be taught the new shape"
        )
    return int(match.group("port")), int(match.group("target"))


class KustomizePlatformServiceTest(unittest.TestCase):
    def test_api_port_matches_the_operator(self):
        port, target = _api_port_from_operator()
        api = next(
            entry
            for entry in _service_manifest()["spec"]["ports"]
            if entry["name"] == "api"
        )
        self.assertEqual(api["port"], port)
        self.assertEqual(
            api["targetPort"],
            target,
            "the Service must target the credential proxy's authenticated listener; "
            "Hermes binds the published port on loopback and validates a different key",
        )

    def test_selector_matches_the_gateway_pod_label(self):
        self.assertEqual(
            _service_manifest()["spec"]["selector"],
            {"app": _AGENT_NAME + _GATEWAY_LABEL_SUFFIX},
        )

    def test_operator_labels_gateway_pods_with_that_suffix(self):
        # The other half of the assertion above: the suffix is the operator's, not
        # this test's, so renaming it there fails here instead of silently leaving
        # the manifest selecting nothing.
        self.assertIn(
            f'"app"] = agent.Name + "{_GATEWAY_LABEL_SUFFIX}"',
            _MANIFESTS_GO.read_text(),
        )

    def test_manifest_is_written_for_that_agent_name(self):
        self.assertEqual(_service_manifest()["metadata"]["name"], _AGENT_NAME)


if __name__ == "__main__":
    unittest.main()
