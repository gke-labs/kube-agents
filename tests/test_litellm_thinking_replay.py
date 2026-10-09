"""The gateway keeps a Gemini model's replayed thinking out of the request, and only Gemini's.

LiteLLM's Gemini transformation sends a replayed thinking block as an ordinary
text part, so Gemini reads its earlier thought summaries as answer text and
repeats them. The chart and the kustomize base mount a pre-call hook that drops
those blocks from assistant messages bound for a Gemini model. These tests
hold the hook's behaviour, that both deployment paths ship the same file and
register it, and that every chart render carries it. The render cases need
`helm` and skip without it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import shutil
import subprocess
import sys
import types
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CHART_FILE = _CHART / "files" / "litellm_thinking_replay_callback.py"
_BASE = _REPO_ROOT / "k8s-operator" / "config" / "integrations" / "litellm" / "base"
_BASE_FILE = _BASE / "litellm_thinking_replay_callback.py"
_CALLBACK = "litellm_thinking_replay_callback.proxy_handler_instance"
_KEY = "litellm_thinking_replay_callback.py"
_MOUNT = "/app/litellm_thinking_replay_callback.py"
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


def _load_hook():
    """Import the hook with a stand-in for LiteLLM's CustomLogger base class."""
    if "litellm.integrations.custom_logger" not in sys.modules:
        litellm = types.ModuleType("litellm")
        integrations = types.ModuleType("litellm.integrations")
        custom_logger = types.ModuleType("litellm.integrations.custom_logger")
        custom_logger.CustomLogger = type("CustomLogger", (), {})
        sys.modules.update(
            {
                "litellm": litellm,
                "litellm.integrations": integrations,
                "litellm.integrations.custom_logger": custom_logger,
            }
        )
    spec = importlib.util.spec_from_file_location("litellm_thinking_replay_callback", _CHART_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Router:
    def __init__(self, models: dict[str, list[str]]):
        self.models = models

    def get_model_list(self, model_name: str):
        return [{"litellm_params": {"model": m}} for m in self.models.get(model_name, [])]


def _session() -> list[dict]:
    return [
        {"role": "user", "content": "how healthy is the namespace"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "**Planning the Check**\n\nI'm going to list pods.", "signature": "sig"},
                {"type": "redacted_thinking", "data": "x"},
                {"type": "text", "text": "Checking."},
                {"type": "tool_use", "id": "t1", "name": "kubectl_get", "input": {"kind": "pods"}},
            ],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        {
            "role": "assistant",
            "content": "Done.",
            "reasoning_content": "kept as a thought part",
            "thinking_blocks": [{"type": "thinking", "thinking": "**Wrapping Up**", "signature": "s2"}],
        },
    ]


class TestHook(unittest.TestCase):
    def setUp(self):
        self.hook = _load_hook()

    def test_gemini_models_are_recognised_by_provider_and_name(self):
        for model in ("gemini/gemini-3.5-flash", "vertex_ai/gemini-3.1-pro-preview"):
            self.assertTrue(self.hook.is_gemini_model(model), model)
        for model in (
            "vertex_ai/claude-opus-5",
            "anthropic/claude-opus-5",
            "openai/gpt-5.4",
            "openai/gemini-3.5-flash",
            "gemini-3.5-flash",
            "",
        ):
            self.assertFalse(self.hook.is_gemini_model(model), model)

    def test_only_an_alias_served_wholly_by_gemini_is_stripped(self):
        router = _Router(
            {
                "model-default": ["vertex_ai/gemini-3.5-flash"],
                "mixed": ["vertex_ai/gemini-3.5-flash", "anthropic/claude-opus-5"],
                "claude": ["vertex_ai/claude-opus-5"],
            }
        )
        self.assertTrue(self.hook.routes_to_gemini("model-default", router))
        self.assertFalse(self.hook.routes_to_gemini("mixed", router))
        self.assertFalse(self.hook.routes_to_gemini("claude", router))
        self.assertFalse(self.hook.routes_to_gemini("unknown", router))
        self.assertFalse(self.hook.routes_to_gemini("model-default", None))

    def test_replayed_thinking_goes_and_everything_else_stays(self):
        messages = _session()
        self.assertEqual(self.hook.strip_replayed_thinking(messages), 3)
        self.assertEqual([b["type"] for b in messages[1]["content"]], ["text", "tool_use"])
        self.assertNotIn("thinking_blocks", messages[3])
        self.assertEqual(messages[3]["reasoning_content"], "kept as a thought part")
        self.assertEqual(messages[3]["content"], "Done.")
        self.assertEqual(messages[0]["content"], "how healthy is the namespace")
        self.assertEqual(self.hook.strip_replayed_thinking(None), 0)

    def test_the_hook_strips_for_gemini_and_leaves_claude_alone(self):
        router = _Router({"model-default": ["vertex_ai/gemini-3.5-flash"], "claude": ["anthropic/claude-opus-5"]})
        self.hook._router = lambda: router
        gemini = {"model": "model-default", "messages": _session()}
        asyncio.run(self.hook.proxy_handler_instance.async_pre_call_hook(None, None, gemini, "anthropic_messages"))
        self.assertEqual([b["type"] for b in gemini["messages"][1]["content"]], ["text", "tool_use"])
        claude = {"model": "claude", "messages": _session()}
        asyncio.run(self.hook.proxy_handler_instance.async_pre_call_hook(None, None, claude, "anthropic_messages"))
        self.assertEqual(claude["messages"], _session())


class TestBothPathsShipIt(unittest.TestCase):
    def test_the_kustomize_copy_is_the_chart_file(self):
        self.assertEqual(_BASE_FILE.read_text(), _CHART_FILE.read_text())

    def test_the_kustomize_base_registers_and_mounts_it(self):
        config = yaml.safe_load((_BASE / "config.yaml").read_text())
        self.assertIn(_CALLBACK, config["litellm_settings"]["callbacks"])
        kustomization = yaml.safe_load((_BASE / "kustomization.yaml").read_text())
        files = next(g["files"] for g in kustomization["configMapGenerator"] if g["name"] == "litellm-config")
        self.assertIn(_KEY, files)
        deployment = yaml.safe_load((_BASE / "deployment.yaml").read_text())
        mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        self.assertIn({"name": "config-volume", "mountPath": _MOUNT, "subPath": _KEY}, mounts)


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestChartRender(unittest.TestCase):
    def _documents(self, extra_args: list[str]) -> list[dict]:
        proc = subprocess.run(_HELM_BASE_ARGS + extra_args, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [d for d in yaml.safe_load_all(proc.stdout) if d]

    def test_every_render_ships_registers_and_mounts_it(self):
        for args in (
            [],
            ["--set", "litellm.otel=true"],
            ["--set-string", "litellm.modelProvider=anthropic"],
            ["--set-string", "litellm.modelProvider=vertex_ai", "--set-string", "litellm.modelDefaultName=claude-opus-5"],
            ["--set", "litellm.redaction.enabled=true"],
        ):
            with self.subTest(args=args):
                documents = self._documents(args)
                configmap = next(d for d in documents if d["kind"] == "ConfigMap")
                self.assertEqual(configmap["data"][_KEY], _CHART_FILE.read_text())
                config = yaml.safe_load(configmap["data"]["config.yaml"])
                self.assertIn(_CALLBACK, config["litellm_settings"]["callbacks"])
                deployment = next(d for d in documents if d["kind"] == "Deployment")
                mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
                self.assertIn({"name": "config-volume", "mountPath": _MOUNT, "subPath": _KEY}, mounts)


if __name__ == "__main__":
    unittest.main()
