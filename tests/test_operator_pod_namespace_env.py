"""The operator learns its own namespace from POD_NAMESPACE, and both install paths set it.

The usage counters poller reaches the agent pods' metrics listeners through a
NetworkPolicy rule the operator renders for its own pods: the namespace it reads
from POD_NAMESPACE and the label app.kubernetes.io/name=kube-agents-operator.
Three things have to agree for that rule to admit anything: the variable name the
operator reads, the Downward-API entry each install path puts on the manager
container, and the label each path puts on the operator's pod. Nothing else holds
them together: the golden tests hand the reconciler a namespace directly, and
`make chart-check` compares neither manifest. A refactor that dropped the variable
would leave the operator logging one line at start-up and every counter still.

Run: python3 -m unittest discover -s tests -p 'test_operator_pod_namespace_env.py' -v
"""

import pathlib
import re
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CHART_YAML = _CHART / "Chart.yaml"
_OPERATOR_TEMPLATE = _CHART / "templates" / "operator-deployment.yaml"
_HELPERS = _CHART / "templates" / "_helpers.tpl"
_MANAGER = _REPO_ROOT / "k8s-operator" / "config" / "manager" / "manager.yaml"
_MANIFESTS_GO = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
_MAIN_GO = _REPO_ROOT / "k8s-operator" / "cmd" / "main.go"

_ENV_CONST = re.compile(r'^\s*OperatorNamespaceEnv\s*=\s*"([A-Z_]+)"', re.M)
_LABEL_CONST = re.compile(r'^\s*operatorPodNameLabel\s*=\s*"([^"]+)"', re.M)
_VALUE_CONST = re.compile(r'^\s*operatorPodNameValue\s*=\s*"([^"]+)"', re.M)
_FIELD_PATH = "metadata.namespace"
_MANAGER_CONTAINER = "manager"


def _go_constant(pattern):
    match = pattern.search(_MANIFESTS_GO.read_text())
    assert match, f"{pattern.pattern} not found in {_MANIFESTS_GO}"
    return match.group(1)


def _chart_env_block(name):
    """The lines of the operator template's env entry for name, as text."""
    text = _OPERATOR_TEMPLATE.read_text()
    match = re.search(rf"^(\s*)- name: {re.escape(name)}\n((?:\1  .*\n)+)", text, re.M)
    assert match, f"{name} is not an env entry in {_OPERATOR_TEMPLATE}"
    return match.group(2)


def _manager_container():
    for doc in yaml.safe_load_all(_MANAGER.read_text()):
        if doc and doc.get("kind") == "Deployment":
            for container in doc["spec"]["template"]["spec"]["containers"]:
                if container["name"] == _MANAGER_CONTAINER:
                    return container, doc["spec"]["template"]["metadata"]["labels"]
    raise AssertionError(f"no {_MANAGER_CONTAINER} container in {_MANAGER}")


class OperatorNamespaceEnvTest(unittest.TestCase):
    def test_the_operator_reads_pod_namespace(self):
        self.assertEqual("POD_NAMESPACE", _go_constant(_ENV_CONST))
        self.assertIn("controller.OperatorNamespaceEnv", _MAIN_GO.read_text(), "main.go does not read the variable")

    def test_the_chart_sets_it_from_the_downward_api(self):
        block = _chart_env_block(_go_constant(_ENV_CONST))
        self.assertIn("valueFrom:", block)
        self.assertIn("fieldRef:", block)
        self.assertIn(f"fieldPath: {_FIELD_PATH}", block)

    def test_kustomize_sets_it_from_the_downward_api(self):
        container, _ = _manager_container()
        entries = {env["name"]: env for env in container.get("env", [])}
        name = _go_constant(_ENV_CONST)
        self.assertIn(name, entries, f"{_MANAGER} does not set {name} on the manager")
        self.assertEqual(_FIELD_PATH, entries[name].get("valueFrom", {}).get("fieldRef", {}).get("fieldPath"))

    def test_both_paths_label_the_operator_pod_as_the_rule_selects(self):
        label, value = _go_constant(_LABEL_CONST), _go_constant(_VALUE_CONST)
        _, labels = _manager_container()
        self.assertEqual(value, labels.get(label), f"{_MANAGER} pod label {label}")
        helpers = _HELPERS.read_text()
        match = re.search(r'define "kube-agents.operatorSelectorLabels"[^}]*}}(.*?){{-?\s*end', helpers, re.S)
        self.assertIsNotNone(match, "operatorSelectorLabels is not defined in _helpers.tpl")
        rendered = match.group(1).replace("{{ .Chart.Name }}", yaml.safe_load(_CHART_YAML.read_text())["name"])
        self.assertIn(f"{label}: {value}", rendered)


if __name__ == "__main__":
    unittest.main()
