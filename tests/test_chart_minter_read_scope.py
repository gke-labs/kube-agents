"""The minter rule ConfigMap the chart renders carries a read-only scope.

The operator renders one read-only policy per `context_repos` entry from the
chart's `default.yaml`, and it renders nothing when that template has no
`platform-agent-read-scope`. So the scope's presence in the chart is what makes
a private context repository readable at all, and its permissions being
`contents: read` alone is what keeps that read from being a write. Both are
asserted on the rendered chart rather than on the template text, because a
template that renders the scope under a different name or with a second
permission would pass a text check and fail the install.
"""

import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_KUSTOMIZE_TEMPLATE = (
    _REPO_ROOT / "k8s-operator" / "config" / "integrations" / "github" / "configmap.yaml.template"
)

_MINTER_CONFIGMAP = "github-token-minter-config"
_WRITE_SCOPE = "platform-agent-scope"
_READ_SCOPE = "platform-agent-read-scope"
_REPO = "gke-fleet-iac"

_HELM_ARGS = [
    "--set",
    "platformAgent.harness.clusterName=ci-cluster",
    "--set",
    "platformAgent.harness.location=us-central1",
    "--set",
    "platformAgent.harness.projectId=ci-project",
    "--set",
    "githubMinter.enabled=true",
    "--set",
    "githubMinter.org=acme",
    "--set",
    f"githubMinter.repo={_REPO}",
    "--set",
    "githubMinter.appId=12345",
]


def _minter_configmap(rendered: str) -> dict:
    for document in yaml.safe_load_all(rendered):
        if (
            isinstance(document, dict)
            and document.get("kind") == "ConfigMap"
            and document.get("metadata", {}).get("name") == _MINTER_CONFIGMAP
        ):
            return document
    raise AssertionError(f"{_MINTER_CONFIGMAP} was not rendered")


def _scopes(policy_text: str) -> dict:
    return yaml.safe_load(policy_text)["scope"]


class MinterReadScopeTest(unittest.TestCase):
    def assert_two_scopes(self, policy_text: str, where: str) -> None:
        scopes = _scopes(policy_text)
        self.assertEqual({_WRITE_SCOPE, _READ_SCOPE}, set(scopes), where)
        read = scopes[_READ_SCOPE]
        self.assertEqual({"contents": "read"}, read["permissions"], f"{where}: the read scope grants a read alone")
        self.assertEqual(scopes[_WRITE_SCOPE]["repositories"], read["repositories"], where)
        # The same caller rule, so the same GSA is the only one either scope answers.
        self.assertEqual(scopes[_WRITE_SCOPE]["rule"], read["rule"], where)
        write = scopes[_WRITE_SCOPE]["permissions"]
        self.assertEqual("write", write["contents"], f"{where}: the write scope is unchanged")

    @unittest.skipUnless(shutil.which("helm"), "helm is not installed")
    def test_the_chart_renders_both_scopes_in_default_and_repo_policies(self):
        proc = subprocess.run(
            ["helm", "template", "test-release", str(_CHART), *_HELM_ARGS],
            capture_output=True,
            text=True,
            check=True,
        )
        data = _minter_configmap(proc.stdout)["data"]
        self.assertEqual({"default.yaml", f"{_REPO}.yaml"}, set(data))
        for key, policy in data.items():
            with self.subTest(key=key):
                self.assert_two_scopes(policy, key)

    def test_the_kustomize_template_carries_the_same_scopes(self):
        # The dev path renders the same ConfigMap through envsubst; a scope the
        # chart has and the template lacks would make the operator render
        # context policies on one install and none on the other.
        text = _KUSTOMIZE_TEMPLATE.read_text(encoding="utf-8")
        substitutions = {
            "${PLATFORM_AGENT_GSA_NAME}": "kubeagents-platform-gsa",
            "${PROJECT_ID}": "ci-project",
            "${GITHUB_REPO}": _REPO,
        }
        for placeholder, value in substitutions.items():
            text = text.replace(placeholder, value)
        data = yaml.safe_load(text)["data"]
        self.assertEqual({"default.yaml", f"{_REPO}.yaml"}, set(data))
        for key, policy in data.items():
            with self.subTest(key=key):
                self.assert_two_scopes(policy, key)


if __name__ == "__main__":
    unittest.main()
