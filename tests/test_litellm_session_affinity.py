"""The gateway gives each Vertex Gemini conversation a stable session_id shared by a few conversations.

Vertex keeps Gemini's implicit prefix cache per serving replica and, on a
project with its LLM_SESSION_ID label, picks the replica from a request's
session_id. The chart's opt-in pre-call hook derives that id from the prompt
prefix and a slot, so every turn of a conversation gets the same id on every
gateway replica, and conversations sharing a prompt share a few warm replicas.
These tests hold the hook's behaviour and that the chart ships it only when
asked on a vertex_ai install. The render cases need `helm` and skip without it.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CHART_FILE = _CHART / "files" / "litellm_session_affinity_callback.py"
_CALLBACK = "litellm_session_affinity_callback.proxy_handler_instance"
_KEY = "litellm_session_affinity_callback.py"
_MOUNT = "/app/litellm_session_affinity_callback.py"
_SLOTS_ENV = "KUBE_AGENTS_SESSION_AFFINITY_SLOTS"
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
_VERTEX_GEMINI = ["--set-string", "litellm.modelProvider=vertex_ai", "--set-string", "litellm.modelDefaultName=gemini-3.1-pro-preview"]
_ON = ["--set", "litellm.vertex.sessionAffinity.enabled=true"]
_SYSTEM = "You are the Platform Agent. " * 600 + "Current time: 12:00."


def _load_hook():
    """Import the hook with a stand-in for LiteLLM's CustomLogger base class."""
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
    spec = importlib.util.spec_from_file_location("litellm_session_affinity_callback", _CHART_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Router:
    def __init__(self, models: dict[str, list[str]]):
        self.models = models

    def get_model_list(self, model_name: str):
        return [{"litellm_params": {"model": m}} for m in self.models.get(model_name, [])]


_ROUTER = _Router(
    {
        "model-default": ["vertex_ai/gemini-3.1-pro-preview"],
        "studio": ["gemini/gemini-3.1-pro-preview"],
        "claude": ["vertex_ai/claude-opus-5"],
        "mixed": ["vertex_ai/gemini-3.1-pro-preview", "vertex_ai/claude-opus-5"],
    }
)


def _request(task: str, turns: int = 1, system: str = _SYSTEM, model: str = "model-default") -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": f"work kanban task {task}"}]
    for i in range(turns - 1):
        messages += [{"role": "assistant", "content": f"step {i}"}, {"role": "user", "content": f"tool output {i}"}]
    return {"model": model, "messages": messages, "tools": [{"type": "function", "function": {"name": "terminal"}}]}


class TestHook(unittest.TestCase):
    def setUp(self):
        self.hook = _load_hook()
        patcher = mock.patch.object(self.hook, "_router", return_value=_ROUTER)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, data: dict, slots: int = 2) -> dict:
        return asyncio.run(self.hook.SessionAffinity(slots=slots).async_pre_call_hook(None, None, data, "completion"))

    def _id(self, data: dict, slots: int = 2):
        return (self._run(data, slots).get("extra_body") or {}).get("session_id")

    def test_every_turn_of_a_conversation_gets_the_same_id(self):
        ids = {self._id(_request("t_1", turns=n)) for n in (1, 2, 5)}
        self.assertEqual(len(ids), 1)

    def test_two_gateway_replicas_agree_with_no_shared_state(self):
        first = self._id(_request("t_1"))
        self.hook = _load_hook()
        with mock.patch.object(self.hook, "_router", return_value=_ROUTER):
            self.assertEqual(self._id(_request("t_1", turns=3)), first)

    def test_conversations_sharing_a_prompt_share_exactly_slots_ids(self):
        for slots in (1, 2, 4):
            with self.subTest(slots=slots):
                ids = {self._id(_request(f"t_{i}"), slots) for i in range(200)}
                self.assertEqual(len(ids), slots)

    def test_a_different_prompt_head_or_tool_list_gets_its_own_ids(self):
        base = {self._id(_request(f"t_{i}")) for i in range(50)}
        other_profile = {self._id(_request(f"t_{i}", system="You are a Cluster Agent. " + _SYSTEM)) for i in range(50)}
        other_tools = set()
        for i in range(50):
            data = _request(f"t_{i}")
            data["tools"] = []
            other_tools.add(self._id(data))
        self.assertFalse(base & other_profile)
        self.assertFalse(base & other_tools)

    def test_text_past_the_prompt_head_does_not_split_the_pool(self):
        tail_a = {self._id(_request(f"t_{i}", system=_SYSTEM + " session a")) for i in range(50)}
        tail_b = {self._id(_request(f"t_{i}", system=_SYSTEM + " session b")) for i in range(50)}
        self.assertEqual(tail_a, tail_b)

    def test_the_id_is_a_signed_64_bit_integer_string(self):
        for i in range(50):
            value = int(self._id(_request(f"t_{i}")))
            self.assertLessEqual(-(2**63), value)
            self.assertLess(value, 2**63)

    def test_a_system_message_added_later_in_the_conversation_keeps_the_id(self):
        # Twenty conversations: a slot that moved would still land on the same
        # slot half the time, so one conversation alone could pass by chance.
        for i in range(20):
            first = self._id(_request(f"t_{i}", turns=3))
            later = _request(f"t_{i}", turns=3)
            later["messages"].append({"role": "system", "content": "context compacted"})
            self.assertEqual(self._id(later), first, f"t_{i}")

    def test_a_messages_api_request_with_a_system_field_gets_an_id(self):
        data = {"model": "model-default", "system": _SYSTEM, "messages": [{"role": "user", "content": "work kanban task t_1"}]}
        self.assertIsNotNone(self._id(data))

    def test_a_callers_own_session_id_and_other_extra_body_keys_are_kept(self):
        data = _request("t_1")
        data["extra_body"] = {"session_id": "42", "labels": {"a": "b"}}
        self.assertEqual(self._run(data)["extra_body"], {"session_id": "42", "labels": {"a": "b"}})
        data = _request("t_1")
        data["extra_body"] = {"labels": {"a": "b"}}
        out = self._run(data)["extra_body"]
        self.assertEqual(out["labels"], {"a": "b"})
        self.assertIn("session_id", out)

    def test_other_models_are_left_alone(self):
        for model in ("studio", "claude", "mixed", "unknown"):
            with self.subTest(model=model):
                self.assertNotIn("extra_body", self._run(_request("t_1", model=model)))

    def test_without_a_router_the_request_is_left_alone(self):
        with mock.patch.object(self.hook, "_router", return_value=None):
            self.assertNotIn("extra_body", self._run(_request("t_1")))

    def test_a_new_conversation_logs_one_diagnostic_line_and_later_turns_none(self):
        with self.assertLogs(self.hook.logger, level="INFO") as logs:
            self._run(_request("t_1"))
            self._run(_request("t_1", turns=3))
        lines = [line for line in logs.output if "new conversation" in line]
        self.assertEqual(len(lines), 1)
        for field in ("head=", "system=", "tools=", "tools_sorted=", "tool_order=", "tool_count=1", "slot=", "session_id="):
            self.assertIn(field, lines[0])
        self.assertNotIn("Platform Agent", lines[0])

    def test_the_logged_slot_and_id_are_the_ones_sent(self):
        data = _request("t_7")
        with self.assertLogs(self.hook.logger, level="INFO") as logs:
            sent = self._run(data)["extra_body"]["session_id"]
        line = next(line for line in logs.output if "new conversation" in line)
        self.assertIn(f"session_id={sent}", line)
        self.assertEqual(self.hook.key_diagnostics(_request("t_7"), 2)["session_id"], sent)
        self.assertEqual(self.hook.key_diagnostics(_request("t_7"), 2)["session_id"], self.hook.session_id_for(_request("t_7"), 2))

    def test_malformed_tools_and_messages_still_get_an_id(self):
        for tools in (None, {"a": 1}, "x", [1, "x", None], [{"function": "x"}], [{"function": None}],
                      [{"function": {"name": 5}}], [{"function": {"name": None}}], [{"name": "a", "input_schema": {}}]):
            with self.subTest(tools=tools):
                data = _request("t_1")
                data["tools"] = tools
                self.assertIsNotNone(self._id(data))
        for messages in ([], [None, 3, "x"], [{"role": "user"}], [{"role": "system", "content": None}]):
            with self.subTest(messages=messages):
                self.assertIsNotNone(self._id({"model": "model-default", "messages": messages}))

    def test_the_diagnostics_read_anthropic_format_tool_names(self):
        tools = [{"name": n, "input_schema": {}} for n in ("a", "b")]
        first, reordered = _request("t_1"), _request("t_1")
        first["tools"], reordered["tools"] = tools, list(reversed(tools))
        d1, d2 = (self.hook.key_diagnostics(d, 2) for d in (first, reordered))
        self.assertEqual(d1["tools_sorted"], d2["tools_sorted"])
        self.assertNotEqual(d1["tool_order"], d2["tool_order"])

    def test_the_diagnostics_tell_a_reordered_tool_list_from_a_changed_one(self):
        tools = [{"type": "function", "function": {"name": n}} for n in ("a", "b")]
        first, reordered, changed = _request("t_1"), _request("t_1"), _request("t_1")
        first["tools"], reordered["tools"] = tools, list(reversed(tools))
        changed["tools"] = [tools[0], {"type": "function", "function": {"name": "b", "description": "x"}}]
        d1, d2, d3 = (self.hook.key_diagnostics(d, 2) for d in (first, reordered, changed))
        self.assertNotEqual(d1["tools"], d2["tools"])
        self.assertEqual(d1["tools_sorted"], d2["tools_sorted"])
        self.assertNotEqual(d1["tool_order"], d2["tool_order"])
        self.assertNotEqual(d1["tools_sorted"], d3["tools_sorted"])
        self.assertEqual(d1["tool_order"], d3["tool_order"])

    def test_the_slot_count_comes_from_the_env_and_a_bad_value_falls_back(self):
        for raw, want in (("", 2), ("3", 3), ("0", 2), ("-1", 2), ("two", 2)):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {_SLOTS_ENV: raw}):
                self.assertEqual(self.hook.slot_count(), want)


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestChartRender(unittest.TestCase):
    def _render(self, extra_args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(_HELM_BASE_ARGS + extra_args, capture_output=True, text=True, check=False)

    def _documents(self, extra_args: list[str]) -> list[dict]:
        proc = self._render(extra_args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return [d for d in yaml.safe_load_all(proc.stdout) if d]

    def _parts(self, extra_args: list[str]):
        documents = self._documents(extra_args)
        configmap = next(d for d in documents if d["kind"] == "ConfigMap")
        callbacks = yaml.safe_load(configmap["data"]["config.yaml"])["litellm_settings"]["callbacks"]
        container = next(d for d in documents if d["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e.get("value") for e in container.get("env", [])}
        return configmap["data"], callbacks, container["volumeMounts"], env

    def test_off_by_default_and_off_for_other_providers(self):
        for args in ([], _VERTEX_GEMINI, _ON, _ON + ["--set-string", "litellm.modelProvider=anthropic"]):
            with self.subTest(args=args):
                data, callbacks, mounts, env = self._parts(args)
                self.assertNotIn(_KEY, data)
                self.assertNotIn(_CALLBACK, callbacks)
                self.assertNotIn(_MOUNT, [m["mountPath"] for m in mounts])
                self.assertNotIn(_SLOTS_ENV, env)

    def test_on_for_vertex_ships_registers_mounts_and_sets_the_slot_count(self):
        for args, slots in ((_VERTEX_GEMINI + _ON, "2"), (_VERTEX_GEMINI + _ON + ["--set", "litellm.vertex.sessionAffinity.slots=3"], "3")):
            with self.subTest(args=args):
                data, callbacks, mounts, env = self._parts(args)
                self.assertEqual(data[_KEY], _CHART_FILE.read_text())
                self.assertIn(_CALLBACK, callbacks)
                self.assertIn({"name": "config-volume", "mountPath": _MOUNT, "subPath": _KEY}, mounts)
                self.assertEqual(env[_SLOTS_ENV], slots)

    def test_turning_it_on_rolls_the_pods(self):
        def checksum(args):
            deployment = next(d for d in self._documents(args) if d["kind"] == "Deployment")
            return deployment["spec"]["template"]["metadata"]["annotations"]

        self.assertNotEqual(checksum(_VERTEX_GEMINI), checksum(_VERTEX_GEMINI + _ON))

    def test_an_edit_to_the_hook_file_alone_rolls_the_pods(self):
        # Turning the hook on also changes config.yaml's callback list, so the
        # on/off comparison above passes even if the hook's source dropped out
        # of the checksum. Change only the file, in a copy of the chart.
        def checksum(chart):
            args = [chart if a == str(_CHART) else a for a in _HELM_BASE_ARGS] + _VERTEX_GEMINI + _ON
            proc = subprocess.run(args, capture_output=True, text=True, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            deployment = next(d for d in yaml.safe_load_all(proc.stdout) if d and d["kind"] == "Deployment")
            return deployment["spec"]["template"]["metadata"]["annotations"]

        with tempfile.TemporaryDirectory() as tmp:
            copy = pathlib.Path(tmp) / "kube-agents"
            shutil.copytree(_CHART, copy)
            before = checksum(str(copy))
            hook = copy / "files" / _KEY
            hook.write_text(hook.read_text() + "\n# edited\n")
            self.assertNotEqual(before, checksum(str(copy)))

    def test_it_runs_after_the_redaction_hook(self):
        # The id leaves the cluster in the Vertex request beside the redacted
        # text; hashed before redaction it would let a short redacted value
        # be recovered by guessing.
        _, callbacks, _, _ = self._parts(_VERTEX_GEMINI + _ON + ["--set", "litellm.redaction.enabled=true"])
        redaction = "litellm_redaction_callback.proxy_handler_instance"
        self.assertGreater(callbacks.index(_CALLBACK), callbacks.index(redaction))

    def test_the_schema_refuses_a_slot_count_below_one(self):
        proc = self._render(_VERTEX_GEMINI + _ON + ["--set", "litellm.vertex.sessionAffinity.slots=0"])
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
