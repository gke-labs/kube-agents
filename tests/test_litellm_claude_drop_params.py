"""The gateway strips the sampling keys Claude refuses, and only for Claude.

Claude Opus 5 answers `temperature`, `top_p` and `top_k` with a 400, and a
caller can send them without knowing which model the gateway routes to. The chart renders LiteLLM's per-deployment `additional_drop_params` under
every `model_list` alias when the model is Claude: `anthropic`, or `vertex_ai`
with a `claude-` model name. Every other provider and model renders no such
key, which keeps a Gemini or OpenAI install's ConfigMap and rollout checksum
unchanged. The render cases need `helm` and skip without it, as
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
_DROPPED = ["temperature", "top_p", "top_k"]
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


def _model_args(provider: str, model: str | None = None) -> list[str]:
    args = ["--set-string", f"litellm.modelProvider={provider}"]
    if model is not None:
        args += ["--set-string", f"litellm.modelDefaultName={model}"]
    return args


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestClaudeDropParamsRender(unittest.TestCase):
    def _config(self, extra_args: list[str]) -> dict:
        proc = subprocess.run(_HELM_BASE_ARGS + extra_args, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        configmap = next(d for d in documents if d["kind"] == "ConfigMap")
        return yaml.safe_load(configmap["data"]["config.yaml"])

    def _params_by_alias(self, extra_args: list[str]) -> dict:
        return {e["model_name"]: e["litellm_params"] for e in self._config(extra_args)["model_list"]}

    def test_claude_drops_the_refused_keys_under_every_alias(self) -> None:
        for args in (
            _model_args("anthropic"),
            _model_args("anthropic", "claude-sonnet-4-5"),
            _model_args("vertex_ai", "claude-opus-5"),
        ):
            with self.subTest(args=args):
                params = self._params_by_alias(args)
                self.assertEqual(len(params), 3)
                for alias, entry in params.items():
                    self.assertEqual(entry.get("additional_drop_params"), _DROPPED, alias)

    def test_other_models_render_no_drop(self) -> None:
        for args in (
            [],
            _model_args("gemini"),
            _model_args("openai"),
            _model_args("vertex_ai"),
            _model_args("vertex_ai", "gemini-3.1-pro-preview"),
            # The prefix test is on the model name, not a substring anywhere.
            _model_args("vertex_ai", "my-claude-tune"),
        ):
            with self.subTest(args=args):
                for alias, entry in self._params_by_alias(args).items():
                    self.assertNotIn("additional_drop_params", entry, alias)

    def test_the_drop_sits_beside_max_tokens(self) -> None:
        params = self._params_by_alias(_model_args("vertex_ai", "claude-opus-5") + ["--set", "litellm.maxTokens=4096"])
        for alias, entry in params.items():
            self.assertEqual(entry.get("max_tokens"), 4096, alias)
            self.assertEqual(entry.get("additional_drop_params"), _DROPPED, alias)


if __name__ == "__main__":
    unittest.main()
