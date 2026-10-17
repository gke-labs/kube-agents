"""`litellm.modelProvider: sap`: SAP AI Core through LiteLLM's own sap provider.

SAP AI Core authenticates with OAuth2 client credentials rather than a static
key, and LiteLLM's sap provider does the token exchange itself from the AICORE_*
environment. So the chart owes two things for this provider, and one thing to
every other install. For sap, every alias routes to sap/<model>, and the gateway
reads all six AICORE_* keys from the credentials Secret as optional references
(either the service key or the four discrete values may be the ones present).
For any other provider, none of that renders: the Deployment, and with it the
rollout, is what it was. The render cases need `helm` and skip without it, as
`test_litellm_max_tokens.py`'s do.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_ALIASES = ("model-default", "hermes-agent")
_SAP_KEYS = (
    "AICORE_SERVICE_KEY",
    "AICORE_CLIENT_ID",
    "AICORE_CLIENT_SECRET",
    "AICORE_AUTH_URL",
    "AICORE_BASE_URL",
    "AICORE_RESOURCE_GROUP",
)
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
    "-s",
    "templates/litellm.yaml",
]


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestSapProviderRender(unittest.TestCase):
    @staticmethod
    def _render(extra_args: list[str] | None = None):
        return subprocess.run(
            _HELM_BASE_ARGS + (extra_args or []), capture_output=True, text=True, check=False
        )

    def _documents(self, extra_args: list[str] | None = None):
        proc = self._render(extra_args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        by_kind = {d["kind"]: d for d in documents if d["kind"] in ("ConfigMap", "Deployment")}
        return by_kind["ConfigMap"], by_kind["Deployment"]

    @staticmethod
    def _models(configmap: dict) -> dict[str, str]:
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        return {entry["model_name"]: entry["litellm_params"]["model"] for entry in config["model_list"]}

    @staticmethod
    def _env(deployment: dict) -> dict[str, dict]:
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        return {entry["name"]: entry for entry in container["env"]}

    def test_every_alias_routes_to_the_sap_default_model(self):
        configmap, _ = self._documents(["--set", "litellm.modelProvider=sap"])
        models = self._models(configmap)
        for alias in _ALIASES:
            self.assertEqual(models[alias], "sap/anthropic--claude-4.5-sonnet", alias)

    def test_a_named_model_replaces_the_default(self):
        configmap, _ = self._documents(
            ["--set", "litellm.modelProvider=sap", "--set-string", "litellm.modelDefaultName=gpt-4o"]
        )
        models = self._models(configmap)
        for alias in _ALIASES:
            self.assertEqual(models[alias], "sap/gpt-4o", alias)
        self.assertEqual(models["gpt-4o"], "sap/gpt-4o")

    def test_the_gateway_reads_every_sap_key_from_the_credentials_secret(self):
        _, deployment = self._documents(
            ["--set", "litellm.modelProvider=sap", "--set-string", "platformAgent.credentials.secretName=custom-creds"]
        )
        env = self._env(deployment)
        for key in _SAP_KEYS:
            self.assertIn(key, env, f"{key} is not in the LiteLLM container's env")
            ref = env[key]["valueFrom"]["secretKeyRef"]
            self.assertEqual(ref["name"], "custom-creds", key)
            self.assertEqual(ref["key"], key, key)
            # Optional, because only one of the two credential shapes is present
            # in any given Secret; a required ref to the absent half would hold
            # the pod in CreateContainerConfigError.
            self.assertIs(ref["optional"], True, key)

    def test_another_provider_renders_no_sap_keys(self):
        for provider in ("gemini", "anthropic", "openai", "vertex_ai"):
            with self.subTest(provider=provider):
                _, deployment = self._documents(["--set", f"litellm.modelProvider={provider}"])
                env = self._env(deployment)
                self.assertFalse([name for name in env if name.startswith("AICORE_")], provider)

    def test_a_claude_model_drops_the_sampling_keys_but_gets_no_cache_markers(self):
        # LiteLLM's sap provider forwards temperature and top_p into the
        # orchestration request, so a Claude model behind SAP needs the same
        # drop as one behind anthropic or vertex_ai. The prompt-caching markers
        # stay off: they have not been shown to pass SAP's orchestration API.
        configmap, _ = self._documents(["--set", "litellm.modelProvider=sap"])
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        for entry in config["model_list"]:
            self.assertEqual(
                entry["litellm_params"].get("additional_drop_params"),
                ["temperature", "top_p", "top_k"],
                entry["model_name"],
            )
        self.assertNotIn("router_settings", config)

    def test_a_non_claude_sap_model_keeps_its_sampling_keys(self):
        configmap, _ = self._documents(
            ["--set", "litellm.modelProvider=sap", "--set-string", "litellm.modelDefaultName=gpt-4o"]
        )
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        for entry in config["model_list"]:
            self.assertNotIn("additional_drop_params", entry["litellm_params"], entry["model_name"])

    def test_an_unknown_provider_names_sap_among_the_choices(self):
        proc = self._render(["--set", "litellm.modelProvider=sap_ai_core"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("openai_compatible, sap", proc.stderr)


if __name__ == "__main__":
    unittest.main()
