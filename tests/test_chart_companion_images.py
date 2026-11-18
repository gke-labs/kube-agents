"""credentialProxy.image and hermesBridge.image render CREDENTIAL_PROXY_IMAGE and
A2A_BRIDGE_IMAGE on the operator only when set (#6190).

The operator derives the credential broker and the hermes-bridge sidecar from the
agent image's registry path and tag. An agent tag the two were never built for (a
dev tag, a mirror that holds the two at another tag) therefore fails the pull, and
before these values the only override was operator.extraEnv, which
global.imageRegistry does not rewrite. These tests pin the contract: both keys
empty renders neither env var; a repository or a tag renders the env var with the
documented repository and Chart.appVersion fallbacks and global.imageRegistry
applied; operator.extraEnv still comes later in the env list, so it wins; and a
render against values without the keys, which `helm upgrade --reuse-values` does,
neither errors nor renders them.

Run: python3 -m unittest discover -s tests -p 'test_chart_companion_images.py' -v
"""

import json
import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_IMAGES_JSON = _REPO_ROOT / "images.json"
_HELM = shutil.which("helm")

_REQUIRED = [
    "--set", "platformAgent.harness.clusterName=ci-cluster",
    "--set", "platformAgent.harness.location=us-central1",
    "--set", "platformAgent.harness.projectId=ci-project",
]
_MANAGER_CONTAINER = "manager"
_MIRROR = "registry.example.com/mirror"
_TAG = "0.33.0"


def _inventory_repository(name: str) -> str:
    """The repository images.json records for the named image."""
    inventory = json.loads(_IMAGES_JSON.read_text())["images"]
    return next(image["repository"] for image in inventory if image["name"] == name)


# The two env vars, each with the values key that renders it and the repository
# the chart falls back to when the key names a tag alone. The template holds
# that fallback as a literal (values.yaml names it in a comment only), so the
# test reads it from images.json, and a repository move there fails the test.
_COMPANIONS = {
    "CREDENTIAL_PROXY_IMAGE": ("credentialProxy", _inventory_repository("credential-proxy")),
    "A2A_BRIDGE_IMAGE": ("hermesBridge", _inventory_repository("hermes-bridge")),
}


def _render(*args) -> list[dict]:
    cmd = [_HELM, "template", "test-release", str(_CHART), *_REQUIRED, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return [doc for doc in yaml.safe_load_all(proc.stdout) if doc is not None]


def _manager_env(*args) -> list[dict]:
    for doc in _render(*args):
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container["name"] == _MANAGER_CONTAINER:
                return container.get("env", [])
    raise AssertionError("the chart rendered no manager container")


def _entries(env: list[dict], name: str) -> list[dict]:
    return [entry for entry in env if entry["name"] == name]


def _app_version() -> str:
    return yaml.safe_load((_CHART / "Chart.yaml").read_text())["appVersion"]


@unittest.skipIf(_HELM is None, "helm is required for chart template rendering tests")
class ChartCompanionImagesTest(unittest.TestCase):
    def test_defaults_render_neither_env_var(self):
        env = _manager_env()
        for name in _COMPANIONS:
            self.assertEqual([], _entries(env, name), name)

    def test_values_without_the_keys_render_neither_and_do_not_error(self):
        """helm upgrade --reuse-values renders against an older release's values,
        where the two top-level keys are nil."""
        env = _manager_env("--set", "credentialProxy=null", "--set", "hermesBridge=null")
        for name in _COMPANIONS:
            self.assertEqual([], _entries(env, name), name)

    def test_repository_and_tag_render_under_the_image_registry(self):
        args = ["--set", f"global.imageRegistry={_MIRROR}"]
        for key, repository in _COMPANIONS.values():
            args += ["--set", f"{key}.image.repository={repository}", "--set", f"{key}.image.tag={_TAG}"]
        env = _manager_env(*args)
        for name, (_, repository) in _COMPANIONS.items():
            trailing = repository.rsplit("/", 1)[-1]
            self.assertEqual([{"name": name, "value": f"{_MIRROR}/{trailing}:{_TAG}"}], _entries(env, name))

    def test_repository_alone_renders_without_a_registry_and_at_app_version(self):
        args = []
        for key, repository in _COMPANIONS.values():
            args += ["--set", f"{key}.image.repository=example.com/team/{repository.rsplit('/', 1)[-1]}"]
        env = _manager_env(*args)
        for name, (_, repository) in _COMPANIONS.items():
            trailing = repository.rsplit("/", 1)[-1]
            self.assertEqual(
                [{"name": name, "value": f"example.com/team/{trailing}:{_app_version()}"}],
                _entries(env, name),
            )

    def test_tag_alone_renders_the_default_repository(self):
        args = []
        for key, _ in _COMPANIONS.values():
            args += ["--set", f"{key}.image.tag={_TAG}"]
        env = _manager_env(*args)
        for name, (_, repository) in _COMPANIONS.items():
            self.assertEqual([{"name": name, "value": f"{repository}:{_TAG}"}], _entries(env, name))

    def test_one_set_renders_that_one_alone(self):
        env = _manager_env("--set", f"hermesBridge.image.tag={_TAG}")
        self.assertEqual([], _entries(env, "CREDENTIAL_PROXY_IMAGE"))
        self.assertEqual(1, len(_entries(env, "A2A_BRIDGE_IMAGE")))

    def test_extra_env_with_the_same_name_comes_later_and_wins(self):
        args = []
        for index, (name, (key, _)) in enumerate(_COMPANIONS.items()):
            args += [
                "--set", f"{key}.image.tag={_TAG}",
                "--set", f"operator.extraEnv[{index}].name={name}",
                "--set", f"operator.extraEnv[{index}].value=override/{name}:1",
            ]
        env = _manager_env(*args)
        for name in _COMPANIONS:
            entries = _entries(env, name)
            self.assertEqual(2, len(entries), name)
            # Kubernetes keeps the last entry of a repeated name.
            self.assertEqual(f"override/{name}:1", entries[-1]["value"])


if __name__ == "__main__":
    unittest.main()
