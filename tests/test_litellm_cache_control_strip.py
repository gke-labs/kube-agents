"""The gateway drops a caller's own cache_control markers on a Gemini request, and only there.

Claude Code marks its prompt-cache breakpoints itself, and the A2A worker
adapter points it at the gateway without knowing the model behind
`model-default`. On a Gemini model LiteLLM turns those markers into a Vertex
explicit-caching create, which gemini-4-argon answers 400
(gke-labs/kube-agents#4318). The chart mounts a pre-call hook that removes the
markers from a request bound for a Gemini model and leaves every other
request, Claude's above all, as sent. These tests hold the hook's behaviour,
that its Gemini gate is the thinking-replay hook's, and that every chart
render ships, registers and mounts it. The render cases need `helm` and skip
without it.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import inspect
import pathlib
import shutil
import subprocess
import sys
import types
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CHART_FILE = _CHART / "files" / "litellm_cache_control_strip_callback.py"
_THINKING_REPLAY_FILE = _CHART / "files" / "litellm_thinking_replay_callback.py"
_CALLBACK = "litellm_cache_control_strip_callback.proxy_handler_instance"
_KEY = "litellm_cache_control_strip_callback.py"
_MOUNT = "/app/litellm_cache_control_strip_callback.py"
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
_EPHEMERAL = {"type": "ephemeral"}


def _stub_litellm() -> None:
    """A stand-in for LiteLLM's CustomLogger base class where LiteLLM is absent."""
    # A real LiteLLM, where installed, is used as it is; the stand-in is only
    # for the environments that have none, as in test_litellm_thinking_replay.py.
    if "litellm" not in sys.modules and importlib.util.find_spec("litellm") is None:
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


def _load(path: pathlib.Path):
    _stub_litellm()
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Router:
    def __init__(self, models: dict[str, list[str]]):
        self.models = models

    def get_model_list(self, model_name: str):
        return [{"litellm_params": {"model": m}} for m in self.models.get(model_name, [])]


def _claude_code_request() -> dict:
    """A /v1/messages body the way Claude Code marks it: system, tools and the tail."""
    return {
        "model": "model-default",
        "system": [
            {"type": "text", "text": "You are Claude Code."},
            {"type": "text", "text": "Project notes.", "cache_control": _EPHEMERAL},
        ],
        "tools": [
            {"name": "Bash", "description": "Run a command", "input_schema": {"type": "object"}},
            {"name": "Read", "description": "Read a file", "input_schema": {"type": "object"}, "cache_control": _EPHEMERAL},
        ],
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "list the pods", "cache_control": _EPHEMERAL}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Listing."},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "kubectl get pods", "cache_control": "data"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": 'a line saying "cache_control"', "cache_control": _EPHEMERAL}],
                        "cache_control": _EPHEMERAL,
                    }
                ],
            },
            {"role": "user", "content": "and the nodes", "cache_control": _EPHEMERAL},
        ],
    }


def _openai_request() -> dict:
    """The same markers in the OpenAI format LiteLLM also accepts them in."""
    return {
        "model": "model-default",
        "messages": [
            {"role": "system", "content": [{"type": "text", "text": "notes", "cache_control": _EPHEMERAL}]},
            {"role": "user", "content": "hi", "cache_control": _EPHEMERAL},
        ],
        "tools": [{"type": "function", "function": {"name": "Read", "parameters": {}, "cache_control": _EPHEMERAL}}],
    }


def _without_markers(request: dict) -> dict:
    """The request with every marker the hook is meant to drop removed, for comparison."""
    expected = copy.deepcopy(request)
    for block in expected.get("system") or []:
        block.pop("cache_control", None)
    for tool in expected.get("tools") or []:
        tool.pop("cache_control", None)
        if isinstance(tool.get("function"), dict):
            tool["function"].pop("cache_control", None)
    for message in expected["messages"]:
        message.pop("cache_control", None)
        if isinstance(message.get("content"), list):
            for block in message["content"]:
                block.pop("cache_control", None)
                if block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                    for inner in block["content"]:
                        inner.pop("cache_control", None)
    return expected


class TestHook(unittest.TestCase):
    def setUp(self):
        self.hook = _load(_CHART_FILE)

    def test_every_marker_claude_code_sets_goes_and_nothing_else_moves(self):
        request = _claude_code_request()
        self.assertEqual(self.hook.strip_cache_control(request), 6)
        self.assertEqual(request, _without_markers(_claude_code_request()))
        # The tool input and the tool result's text are the caller's data.
        self.assertEqual(request["messages"][1]["content"][1]["input"]["cache_control"], "data")
        self.assertIn("cache_control", request["messages"][2]["content"][0]["content"][0]["text"])

    def test_the_openai_format_markers_go_too(self):
        request = _openai_request()
        self.assertEqual(self.hook.strip_cache_control(request), 3)
        self.assertEqual(request, _without_markers(_openai_request()))

    def test_a_request_without_markers_or_shape_is_left_alone(self):
        plain = {"model": "model-default", "system": "a string system", "messages": [{"role": "user", "content": "hi"}]}
        before = copy.deepcopy(plain)
        self.assertEqual(self.hook.strip_cache_control(plain), 0)
        self.assertEqual(plain, before)
        for odd in (None, [], {"messages": "not a list"}, {"messages": [None, "text"], "tools": [1], "system": 3}):
            with self.subTest(odd=odd):
                self.assertEqual(self.hook.strip_cache_control(odd), 0)

    def test_the_hook_strips_for_gemini_and_leaves_claude_alone(self):
        router = _Router({"model-default": ["vertex_ai/gemini-4-argon"], "claude": ["vertex_ai/claude-opus-5"]})
        self.hook._router = lambda: router
        gemini = _claude_code_request()
        with self.assertLogs("kube_agents.litellm_cache_control_strip", level="INFO") as logs:
            asyncio.run(self.hook.proxy_handler_instance.async_pre_call_hook(None, None, gemini, "anthropic_messages"))
        self.assertEqual(gemini, _without_markers(_claude_code_request()))
        self.assertIn("dropped 6 cache_control marker(s)", logs.output[0])
        claude = _claude_code_request()
        claude["model"] = "claude"
        asyncio.run(self.hook.proxy_handler_instance.async_pre_call_hook(None, None, claude, "anthropic_messages"))
        expected = _claude_code_request()
        expected["model"] = "claude"
        self.assertEqual(claude, expected)

    def test_a_blind_hook_leaves_the_request_alone_and_says_so_once(self):
        self.hook._router = lambda: None
        self.hook._blind_warned = False
        request = _claude_code_request()
        with self.assertLogs("kube_agents.litellm_cache_control_strip", level="WARNING") as logs:
            asyncio.run(self.hook.proxy_handler_instance.async_pre_call_hook(None, None, request, "anthropic_messages"))
            asyncio.run(self.hook.proxy_handler_instance.async_pre_call_hook(None, None, request, "anthropic_messages"))
        self.assertEqual(request, _claude_code_request())
        self.assertEqual(len(logs.records), 1)
        self.assertIn("hook inactive", logs.output[0])

    def test_the_gemini_gate_is_the_thinking_replay_hooks(self):
        """One decision of what a Gemini alias is, in two files the proxy loads separately."""
        other = _load(_THINKING_REPLAY_FILE)
        for name in ("is_gemini_model", "routes_to_gemini"):
            with self.subTest(name=name):
                mine = inspect.getsource(getattr(self.hook, name))
                theirs = inspect.getsource(getattr(other, name))
                self.assertEqual(mine, theirs)
        for name in ("GEMINI_PROVIDERS", "GEMINI_MODEL_PREFIX", "PROVIDER_SEPARATOR", "DEPLOYMENT_PARAMS_KEY", "DEPLOYMENT_MODEL_KEY"):
            with self.subTest(name=name):
                self.assertEqual(getattr(self.hook, name), getattr(other, name))


@unittest.skipUnless(importlib.util.find_spec("litellm"), "litellm is not installed")
class TestAgainstARealRouter(unittest.TestCase):
    def test_the_proxy_loads_it_by_its_callbacks_string_and_it_reads_the_proxy_router(self):
        import litellm.proxy.proxy_server as proxy_server
        from litellm import Router
        from litellm.proxy.types_utils.utils import get_instance_fn

        instance = get_instance_fn(_CALLBACK, config_file_path=str(_CHART_FILE.parent / "config.yaml"))
        router = Router(model_list=[{"model_name": "model-default", "litellm_params": {"model": "vertex_ai/gemini-4-argon"}}])
        previous = proxy_server.llm_router
        proxy_server.llm_router = router
        try:
            data = _claude_code_request()
            asyncio.run(instance.async_pre_call_hook(None, None, data, "anthropic_messages"))
        finally:
            proxy_server.llm_router = previous
        self.assertEqual(data, _without_markers(_claude_code_request()))


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
            ["--set-string", "litellm.modelProvider=vertex_ai", "--set-string", "litellm.modelDefaultName=gemini-4-argon"],
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

    def test_an_edit_to_the_hook_rolls_the_pods(self):
        """The file feeds the Deployment's config checksum, as every subPath mount must."""
        proc = subprocess.run(_HELM_BASE_ARGS, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        before = next(d for d in yaml.safe_load_all(proc.stdout) if d and d["kind"] == "Deployment")
        checksum = before["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        original = _CHART_FILE.read_text()
        try:
            _CHART_FILE.write_text(original + "\n# edited\n")
            proc = subprocess.run(_HELM_BASE_ARGS, capture_output=True, text=True, check=False)
        finally:
            _CHART_FILE.write_text(original)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = next(d for d in yaml.safe_load_all(proc.stdout) if d and d["kind"] == "Deployment")
        self.assertNotEqual(after["spec"]["template"]["metadata"]["annotations"]["checksum/config"], checksum)


if __name__ == "__main__":
    unittest.main()
