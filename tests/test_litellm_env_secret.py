"""Helm render tests for `litellm.envFromSecret`, the `sap` provider and provider slugs.

`litellm.envFromSecret` names a Secret the gateway container reads whole as
environment variables, which is how SAP AI Core (`sap`), any other LiteLLM
provider slug and an OAuth2 gateway get their credentials. These tests drive
`helm template` and assert: the envFrom entry renders only when the value is
set, optional and under every provider; a bad name fails the render; `sap`
takes its default model and routes every alias to `sap/<model>`; any other
slug renders with a model name and fails without one, as does a value that is
not a slug; and with the value unset every provider renders exactly what it
rendered before. They need `helm` and skip without it.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_HELM_BASE_ARGS = [
    "helm",
    "template",
    "test-release",
    str(_CHART),
    "--set-string",
    "platformAgent.harness.clusterName=test-cluster",
    "--set-string",
    "platformAgent.harness.location=us-central1",
    "--set-string",
    "platformAgent.harness.projectId=test-project",
]
_SECRET = "sap-aicore"
_SAP_DEFAULT_MODEL = "anthropic--claude-4.5-sonnet"
_ALIASES = ("model-default", "hermes-agent")
_EXISTING_PROVIDERS = (
    ["--set", "litellm.modelProvider=gemini"],
    ["--set", "litellm.modelProvider=anthropic"],
    ["--set", "litellm.modelProvider=openai"],
    ["--set", "litellm.modelProvider=vertex_ai"],
    ["--set", "litellm.modelProvider=openai_compatible", "--set", "litellm.modelDefaultName=m",
     "--set", "litellm.openaiCompatible.apiBase=https://llm.example.com/v1"],
)


def _helm(*extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(_HELM_BASE_ARGS + list(extra_args), capture_output=True, text=True, check=False)


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestEnvFromSecretRender(unittest.TestCase):
    def _render(self, *extra_args: str) -> list[dict]:
        proc = _helm(*extra_args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [d for d in yaml.safe_load_all(proc.stdout) if d]

    def _gateway_container(self, docs: list[dict]) -> dict:
        deployment = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "litellm")
        return deployment["spec"]["template"]["spec"]["containers"][0]

    def _config(self, docs: list[dict]) -> dict:
        configmap = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
        return yaml.safe_load(configmap["data"]["config.yaml"])

    def _assert_fails(self, fragment: str, *extra_args: str) -> None:
        proc = _helm(*extra_args)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(fragment, proc.stderr)

    def test_the_secret_is_mounted_optional_under_every_provider(self) -> None:
        cases = list(_EXISTING_PROVIDERS) + [["--set", "litellm.modelProvider=sap"]]
        for args in cases:
            with self.subTest(args=args):
                container = self._gateway_container(self._render(*args, "--set", f"litellm.envFromSecret={_SECRET}"))
                self.assertEqual(container["envFrom"], [{"secretRef": {"name": _SECRET, "optional": True}}])

    def test_a_name_kubernetes_would_refuse_fails_the_render(self) -> None:
        for name in ("Sap-Aicore", "sap_aicore", "-sap", "a" * 254):
            with self.subTest(name=name):
                self._assert_fails("litellm.envFromSecret", "--set", f"litellm.envFromSecret={name}")

    def test_sap_takes_its_default_model_and_routes_every_alias_to_it(self) -> None:
        params = {e["model_name"]: e["litellm_params"] for e in self._config(self._render("--set", "litellm.modelProvider=sap"))["model_list"]}
        self.assertEqual(set(params), set(_ALIASES) | {_SAP_DEFAULT_MODEL})
        for alias, entry in params.items():
            self.assertEqual(entry["model"], f"sap/{_SAP_DEFAULT_MODEL}", alias)
            self.assertNotIn("api_base", entry, alias)

    def test_sap_keeps_the_saas_keys_optional_and_adds_nothing_else(self) -> None:
        # The same env block every table provider but openai_compatible gets:
        # the three SaaS keys, each optional; nothing SAP-specific is wired by
        # name, since the env Secret carries whatever LiteLLM's sap driver reads.
        container = self._gateway_container(self._render("--set", "litellm.modelProvider=sap"))
        names = [e["name"] for e in container["env"]]
        for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            self.assertIn(key, names)
        self.assertFalse(any(n.startswith("AICORE_") for n in names), names)

    def test_another_slug_renders_with_a_model_and_fails_without_one(self) -> None:
        params = {e["model_name"]: e["litellm_params"] for e in self._config(self._render(
            "--set", "litellm.modelProvider=azure", "--set", "litellm.modelDefaultName=gpt-5.4"))["model_list"]}
        self.assertEqual(params["model-default"]["model"], "azure/gpt-5.4")
        self._assert_fails("litellm.modelDefaultName", "--set", "litellm.modelProvider=azure")

    def test_a_value_that_is_not_a_slug_fails_naming_the_table(self) -> None:
        for provider in ("v-llm", "Azure", "1azure", "sap/", "azure bedrock"):
            with self.subTest(provider=provider):
                self._assert_fails("vertex_ai, openai_compatible, sap", "--set-string", f"litellm.modelProvider={provider}",
                                   "--set", "litellm.modelDefaultName=m")

    def test_existing_providers_render_what_they_did_with_the_value_unset(self) -> None:
        # The value's default is the empty string, so the render has no
        # envFrom, no OAuth2 hook and the same checksum as before the value
        # existed; this pins the first two, test_litellm_oauth2_callback.py's
        # render case pins the hook for openai_compatible.
        for args in _EXISTING_PROVIDERS:
            with self.subTest(args=args):
                docs = self._render(*args)
                container = self._gateway_container(docs)
                self.assertNotIn("envFrom", container)
                self.assertFalse(any(m.get("subPath") == "litellm_oauth2_callback.py" for m in container["volumeMounts"]))
                self.assertNotIn("litellm_oauth2_callback", str(self._config(docs)["litellm_settings"]["callbacks"]))


if __name__ == "__main__":
    unittest.main()
