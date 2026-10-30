"""Tests for litellm.vertexSessionAffinity and the Vertex session-affinity hook.

1. The hook: a vertex_ai Gemini chat completion gets extra_body.sessionId, the
   same first message gives the same id across a session's calls, and Claude,
   the gemini provider and other call types go out as sent; a failure inside
   the hook leaves the request unchanged and never raises.
2. Where LiteLLM is installed, the id reaches the top level of the Vertex
   request body LiteLLM builds.
3. The chart: off by default with the default render unchanged; on, the
   ConfigMap carries the file verbatim, the callback is registered, the file
   is mounted beside config.yaml and the pods roll; the schema takes booleans
   only. These need `helm` and skip without it.
4. The eval install turns it on (hack/ci-deploy.sh).
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
_CHART_FILE = _CHART / "files" / "litellm_vertex_session_callback.py"
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_CALLBACK = "litellm_vertex_session_callback.proxy_handler_instance"
_KEY = "litellm_vertex_session_callback.py"
_MOUNT = "/app/litellm_vertex_session_callback.py"
_LOGGER = "kube_agents.litellm_vertex_session"
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
_GEMINI = "vertex_ai/gemini-3.1-pro-preview"


def _load_hook():
    """Import the hook with a stand-in for LiteLLM's CustomLogger base class where LiteLLM is absent."""
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
    spec = importlib.util.spec_from_file_location("litellm_vertex_session_callback", _CHART_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Router:
    def __init__(self, models: dict[str, list[str]]):
        self.models = models

    def get_model_list(self, model_name: str):
        return [{"litellm_params": {"model": m}} for m in self.models.get(model_name, [])]


class _BrokenRouter:
    def get_model_list(self, model_name: str):
        raise RuntimeError("router changed shape")


_ROUTER = _Router(
    {
        "model-default": [_GEMINI],
        "hermes-agent": [_GEMINI],
        "claude": ["vertex_ai/claude-opus-5-5"],
        "studio": ["gemini/gemini-3.1-pro-preview"],
        "mixed": [_GEMINI, "vertex_ai/claude-opus-5-5"],
        "garden": ["vertex_ai/publishers/google/models/gemini-3.1-pro-preview"],
    }
)


def _request(model: str = "model-default", first: object = "work kanban task t_123", turns: int = 1) -> dict:
    messages = [{"role": "system", "content": "you are the platform agent"}, {"role": "user", "content": first}]
    for index in range(turns - 1):
        messages.append({"role": "assistant", "content": f"step {index}"})
        messages.append({"role": "tool", "content": f"result {index}"})
    return {"model": model, "messages": messages}


class TestHook(unittest.TestCase):
    def setUp(self):
        self.hook = _load_hook()
        self.hook._router = lambda: _ROUTER

    def _run(self, data: dict, call_type: str = "acompletion") -> dict:
        return asyncio.run(self.hook.proxy_handler_instance.async_pre_call_hook(None, None, data, call_type))

    def test_a_vertex_gemini_completion_carries_a_session_id(self):
        data = self._run(_request())
        sid = data["extra_body"]["sessionId"]
        self.assertRegex(sid, r"^[0-9a-f]{32}$")

    def test_a_model_garden_gemini_path_counts(self):
        self.assertIn("sessionId", self._run(_request("garden"))["extra_body"])

    def test_one_session_keeps_its_id_across_calls_and_aliases(self):
        first = self._run(_request(turns=1))["extra_body"]["sessionId"]
        later = self._run(_request(turns=5))["extra_body"]["sessionId"]
        aliased = self._run(_request("hermes-agent", turns=3))["extra_body"]["sessionId"]
        self.assertEqual(first, later)
        self.assertEqual(first, aliased)

    def test_a_different_first_message_gets_a_different_id(self):
        one = self._run(_request(first="work kanban task t_1"))["extra_body"]["sessionId"]
        two = self._run(_request(first="work kanban task t_2"))["extra_body"]["sessionId"]
        self.assertNotEqual(one, two)

    def test_the_system_prompt_does_not_move_the_id(self):
        one = _request()
        two = _request()
        two["messages"][0]["content"] = "a different system prompt"
        self.assertEqual(
            self._run(one)["extra_body"]["sessionId"],
            self._run(two)["extra_body"]["sessionId"],
        )

    def test_list_content_hashes_as_its_text(self):
        plain = self._run(_request(first="work kanban task t_9"))["extra_body"]["sessionId"]
        parts = self._run(
            _request(first=[{"type": "text", "text": "work kanban task t_9", "cache_control": {"type": "ephemeral"}}])
        )["extra_body"]["sessionId"]
        self.assertEqual(plain, parts)

    def test_the_id_matches_its_documented_recipe(self):
        import hashlib

        expected = hashlib.sha256(f"{_GEMINI}\nwork kanban task t_123".encode()).hexdigest()[:32]
        self.assertEqual(self._run(_request())["extra_body"]["sessionId"], expected)

    def test_claude_the_gemini_provider_and_mixed_aliases_go_out_as_sent(self):
        for model in ("claude", "studio", "mixed", "unknown"):
            with self.subTest(model=model):
                self.assertEqual(self._run(_request(model)), _request(model))

    def test_other_call_types_go_out_as_sent(self):
        for call_type in ("anthropic_messages", "aembedding", "aresponses"):
            with self.subTest(call_type=call_type):
                self.assertEqual(self._run(_request(), call_type), _request())

    def test_a_request_with_no_user_turn_goes_out_as_sent(self):
        data = {"model": "model-default", "messages": [{"role": "system", "content": "only a system prompt"}]}
        self.assertNotIn("extra_body", self._run(dict(data)))

    def test_a_callers_own_extra_body_and_session_id_are_kept(self):
        data = _request()
        data["extra_body"] = {"labels": {"team": "evals"}}
        out = self._run(data)["extra_body"]
        self.assertEqual(out["labels"], {"team": "evals"})
        self.assertIn("sessionId", out)
        data = _request()
        data["extra_body"] = {"sessionId": "caller-chosen"}
        self.assertEqual(self._run(data)["extra_body"]["sessionId"], "caller-chosen")

    def test_no_router_leaves_the_request_alone(self):
        self.hook._router = lambda: None
        self.assertEqual(self._run(_request()), _request())

    def test_a_failure_inside_the_hook_warns_once_and_never_raises(self):
        self.hook._router = lambda: _BrokenRouter()
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            self.assertEqual(self._run(_request()), _request())
            self.assertEqual(self._run(_request()), _request())
        self.assertEqual(len(logs.records), 1)
        self.assertIn("without a sessionId", logs.output[0])

    def test_an_unreadable_message_list_never_raises(self):
        for messages in (None, "not a list", [None, 5], [{"role": "user", "content": object()}]):
            with self.subTest(messages=messages):
                data = {"model": "model-default", "messages": messages}
                self._run(data)


@unittest.skipUnless(importlib.util.find_spec("litellm"), "litellm is not installed")
class TestAgainstLiteLLM(unittest.TestCase):
    """The id reaches the Vertex request body LiteLLM itself builds."""

    def test_the_vertex_body_carries_a_top_level_session_id(self):
        from litellm.llms.vertex_ai.gemini.transformation import transform_request_body
        from litellm.utils import get_optional_params

        hook = _load_hook()
        hook._router = lambda: _ROUTER
        data = asyncio.run(hook.proxy_handler_instance.async_pre_call_hook(None, None, _request(), "acompletion"))
        optional_params = get_optional_params(
            model="gemini-3.1-pro-preview",
            custom_llm_provider="vertex_ai",
            extra_body=data["extra_body"],
        )
        body = transform_request_body(
            messages=data["messages"],
            model="gemini-3.1-pro-preview",
            optional_params=optional_params,
            custom_llm_provider="vertex_ai",
            litellm_params={},
            cached_content=None,
        )
        self.assertEqual(body["sessionId"], data["extra_body"]["sessionId"])


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestChartRender(unittest.TestCase):
    def _documents(self, extra_args: list[str] | None = None):
        proc = subprocess.run(_HELM_BASE_ARGS + (extra_args or []), capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        by_kind = {d["kind"]: d for d in documents if d["kind"] in ("ConfigMap", "Deployment")}
        return by_kind["ConfigMap"], by_kind["Deployment"]

    @staticmethod
    def _checksum(deployment: dict) -> str:
        return deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

    @staticmethod
    def _callbacks(configmap: dict) -> list[str]:
        return yaml.safe_load(configmap["data"]["config.yaml"])["litellm_settings"]["callbacks"]

    @staticmethod
    def _mounts(deployment: dict) -> list[dict]:
        return deployment["spec"]["template"]["spec"]["containers"][0].get("volumeMounts", [])

    def test_default_unset_and_explicit_false_are_identical_and_carry_no_hook(self):
        unset_cm, unset_deploy = self._documents()
        false_cm, false_deploy = self._documents(["--set", "litellm.vertexSessionAffinity=false"])
        self.assertEqual(unset_cm, false_cm)
        self.assertEqual(self._checksum(unset_deploy), self._checksum(false_deploy))
        self.assertNotIn(_KEY, unset_cm["data"])
        self.assertNotIn(_CALLBACK, self._callbacks(unset_cm))
        self.assertFalse(any(m.get("mountPath") == _MOUNT for m in self._mounts(unset_deploy)))

    def test_enabled_renders_file_callback_mount_and_rolls_the_pods(self):
        args = ["--set-string", "litellm.modelProvider=vertex_ai", "--set", "litellm.vertexSessionAffinity=true"]
        set_cm, set_deploy = self._documents(args)
        _, unset_deploy = self._documents(args[:2])
        self.assertEqual(set_cm["data"][_KEY], _CHART_FILE.read_text(encoding="utf-8"))
        callbacks = self._callbacks(set_cm)
        self.assertIn(_CALLBACK, callbacks)
        self.assertIn("litellm_thinking_replay_callback.proxy_handler_instance", callbacks)
        self.assertIn({"name": "config-volume", "mountPath": _MOUNT, "subPath": _KEY}, self._mounts(set_deploy))
        self.assertNotEqual(self._checksum(unset_deploy), self._checksum(set_deploy))

    def test_schema_takes_booleans_only(self):
        proc = subprocess.run(
            _HELM_BASE_ARGS + ["--set-string", "litellm.vertexSessionAffinity=yes"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("vertexSessionAffinity", proc.stderr)


class TestEvalSetting(unittest.TestCase):
    def test_the_eval_install_turns_it_on(self):
        self.assertIn('--set "litellm.vertexSessionAffinity=true"', _CI_DEPLOY.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
