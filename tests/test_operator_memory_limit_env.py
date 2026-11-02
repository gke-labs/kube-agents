"""The manager sets its Go soft memory limit from OPERATOR_MEMORY_LIMIT_BYTES, and both
install paths set it from the container's own limits.memory.

The manager derives the soft limit from the byte count the Downward API writes to the
variable (internal/memlimit); with the variable absent it logs one line and the runtime
default stands, which is how the operator ran before it. Three things have to agree for the
limit to follow operator.resources: the variable name the manager reads, and the
resourceFieldRef each install path puts on the manager container. `make chart-check`
compares neither manifest, so this test does.

Run: python3 -m unittest discover -s tests -p 'test_operator_memory_limit_env.py' -v
"""

import pathlib
import re
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_OPERATOR_TEMPLATE = _REPO_ROOT / "charts" / "kube-agents" / "templates" / "operator-deployment.yaml"
_MANAGER = _REPO_ROOT / "k8s-operator" / "config" / "manager" / "manager.yaml"
_MAIN_GO = _REPO_ROOT / "k8s-operator" / "cmd" / "main.go"

_ENV_CONST = re.compile(r'^\s*operatorMemoryLimitEnv\s*=\s*"([A-Z_]+)"', re.M)
_RESOURCE = "limits.memory"
_MANAGER_CONTAINER = "manager"


def _env_name():
    match = _ENV_CONST.search(_MAIN_GO.read_text())
    assert match, f"operatorMemoryLimitEnv not found in {_MAIN_GO}"
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
                    return container
    raise AssertionError(f"no {_MANAGER_CONTAINER} container in {_MANAGER}")


class OperatorMemoryLimitEnvTest(unittest.TestCase):
    def test_the_manager_applies_the_limit_from_the_variable(self):
        main_go = _MAIN_GO.read_text()
        self.assertEqual("OPERATOR_MEMORY_LIMIT_BYTES", _env_name())
        self.assertIn("memlimit.Apply(operatorMemoryLimitEnv", main_go, "main.go does not apply the limit")

    def test_the_chart_sets_it_from_the_containers_memory_limit(self):
        block = _chart_env_block(_env_name())
        self.assertIn("valueFrom:", block)
        self.assertIn("resourceFieldRef:", block)
        self.assertIn(f"resource: {_RESOURCE}", block)

    def test_kustomize_sets_it_from_the_containers_memory_limit(self):
        container = _manager_container()
        entries = {env["name"]: env for env in container.get("env", [])}
        name = _env_name()
        self.assertIn(name, entries, f"{_MANAGER} does not set {name} on the manager")
        ref = entries[name].get("valueFrom", {}).get("resourceFieldRef", {})
        self.assertEqual(_RESOURCE, ref.get("resource"))
        # No containerName: the field refers to the container it is set on, so a renamed
        # container cannot leave it pointing at a name that no longer exists.
        self.assertNotIn("containerName", ref)


if __name__ == "__main__":
    unittest.main()
