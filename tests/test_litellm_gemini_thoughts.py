"""The gateway asks a Gemini model that thinks for its thoughts, and no other model.

Gemini thinks by default but returns its thoughts only when the request sets
thinkingConfig.includeThoughts; with it off, Gemini 3.x sometimes writes its
reasoning into the answer under a first line `thought`. The chart renders
`thinking: {"type": "enabled"}` under every model_list alias for a Gemini 2.5
or 3.x model through gemini or vertex_ai, which LiteLLM v1.106.0 maps to
includeThoughts and nothing else. Three things have to hold: the key is on
every alias of such a model; it is absent for every other model, because
LiteLLM refuses it on a model it does not treat as a reasoning model and on
Claude it would turn thinking on; and it gives way to `litellm.reasoningEffort`,
because the two on one Gemini 3.x deployment fail every request. The chart's
comment in _helpers.tpl (kube-agents.litellmConfig) says why. The render cases
need `helm` and skip without it, as the other litellm render tests do.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_KUSTOMIZE_BASE_CONFIG = _REPO_ROOT / "k8s-operator" / "config" / "integrations" / "litellm" / "base" / "config.yaml"
_THINKING_KEY = "thinking"
_THINKING = {"type": "enabled"}
_ALIAS_COUNT = 3
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


def _model_args(provider: str | None = None, model: str | None = None) -> list[str]:
    args: list[str] = []
    if provider is not None:
        args += ["--set-string", f"litellm.modelProvider={provider}"]
    if model is not None:
        args += ["--set-string", f"litellm.modelDefaultName={model}"]
    return args


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestGeminiThoughtsRender(unittest.TestCase):
    def _params_by_alias(self, extra_args: list[str]) -> dict:
        proc = subprocess.run(_HELM_BASE_ARGS + extra_args, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        configmap = next(d for d in documents if d["kind"] == "ConfigMap")
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        params = {entry["model_name"]: entry["litellm_params"] for entry in config["model_list"]}
        self.assertEqual(len(params), _ALIAS_COUNT)
        return params

    def test_a_gemini_model_that_thinks_gets_it_on_every_alias(self) -> None:
        for args in (
            [],
            _model_args("gemini"),
            _model_args("vertex_ai"),
            _model_args("gemini", "gemini-2.5-pro"),
            _model_args("vertex_ai", "gemini-3.1-pro-preview"),
            # LiteLLM counts the -latest aliases as Gemini 3.x.
            _model_args("gemini", "gemini-flash-latest"),
            # A Model Garden path: the name is read from its last segment.
            _model_args("vertex_ai", "publishers/google/models/gemini-3.5-flash"),
        ):
            with self.subTest(args=args):
                for alias, entry in self._params_by_alias(args).items():
                    with self.subTest(alias=alias):
                        self.assertEqual(entry[_THINKING_KEY], _THINKING)

    def test_every_other_model_gets_none(self) -> None:
        for args in (
            # LiteLLM does not treat these as reasoning models and refuses the key.
            _model_args("gemini", "gemini-2.0-flash"),
            _model_args("vertex_ai", "gemini-1.5-pro"),
            _model_args("gemini", "gemma-3-27b-it"),
            _model_args("gemini", "gemini-flash-latest-preview"),
            # On Claude it would turn thinking on.
            _model_args("vertex_ai", "claude-opus-5"),
            _model_args("anthropic"),
            _model_args("openai"),
            # A Gemini name behind another provider is that provider's model.
            _model_args("openai_compatible", "gemini-3.5-flash")
            + [
                "--set-string",
                "litellm.openaiCompatible.apiBase=http://vllm.models.svc.cluster.local:8000/v1",
                "--set",
                "litellm.openaiCompatible.targetPort=8000",
            ],
        ):
            with self.subTest(args=args):
                for alias, entry in self._params_by_alias(args).items():
                    with self.subTest(alias=alias):
                        self.assertNotIn(_THINKING_KEY, entry)

    def test_a_reasoning_effort_replaces_it(self) -> None:
        for provider in ("gemini", "vertex_ai"):
            with self.subTest(provider=provider):
                params = self._params_by_alias(
                    _model_args(provider) + ["--set-string", "litellm.reasoningEffort=low"]
                )
                for alias, entry in params.items():
                    with self.subTest(alias=alias):
                        self.assertNotIn(_THINKING_KEY, entry)
                        self.assertEqual(entry["reasoning_effort"], "low")


class TestKustomizeBase(unittest.TestCase):
    def test_the_base_carries_no_thinking_key(self) -> None:
        # The base picks no overlay by model and also serves openai, which
        # refuses the key, so the setting is chart-only.
        config = yaml.safe_load(_KUSTOMIZE_BASE_CONFIG.read_text())
        for entry in config["model_list"]:
            self.assertNotIn(_THINKING_KEY, entry["litellm_params"])


if __name__ == "__main__":
    unittest.main()
