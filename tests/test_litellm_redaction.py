"""The gateway redaction hook: the chart copy, the callback, and the render.

Three things have to hold together for `litellm.redaction` to do anything:
the redactor module the chart mounts must be the one the chat plugins test
(`charts/kube-agents/files/redactor.py` is a copy, and a copy drifts); the
LiteLLM pre-call hook must actually rewrite the shapes an OpenAI-wire request
carries; and the chart must deliver the hook, its module and its rule file to
the same directory as `config.yaml`, because LiteLLM resolves a custom
callback relative to that file. The render cases need `helm` and skip without
it, as `test_minter_repo_wiring.py`'s do; the parity and callback cases run
everywhere.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

import yaml
from fastapi import HTTPException

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CHART_FILES = _CHART / "files"
_PLUGIN_REDACTOR = _REPO_ROOT / "agents" / "chat" / "defaults" / "plugins" / "common" / "redactor.py"
_CHART_REDACTOR = _CHART_FILES / "redactor.py"
_CALLBACK = _CHART_FILES / "litellm_redaction_callback.py"
_CALLBACK_INSTANCE = "litellm_redaction_callback.proxy_handler_instance"
_CONFIG_ENV_VAR = "KUBE_AGENTS_REDACTION_CONFIG"
_SALT_ENV_VAR = "SESSION_KV_SALT"
_CONFIGMAP_KEYS = ("redaction.yaml", "redactor.py", "litellm_redaction_callback.py")
# What every render carries whether redaction is on or off: the config, the
# thinking-replay hook (tests/test_litellm_thinking_replay.py) and the
# cache-marker hook (tests/test_litellm_cache_control_strip.py).
_ALWAYS_KEYS = (
    "config.yaml",
    "litellm_thinking_replay_callback.py",
    "litellm_cache_control_strip_callback.py",
    "litellm_caller_auth.py",
)
_ALWAYS_CALLBACKS = [
    "prometheus",
    "litellm_thinking_replay_callback.proxy_handler_instance",
    "litellm_cache_control_strip_callback.proxy_handler_instance",
]
_MOUNT_DIR = "/app"
# The ConfigMap volume. The container also mounts an emptyDir scratch
# directory that carries no subPath, so mounts are read by volume, not by
# position (tests/test_workload_security_context.py owns that volume).
_CONFIG_VOLUME = "config-volume"
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
_RULES_CONFIG = {
    "ip": {"action": "pseudonym", "allowCidrs": ["127.0.0.0/8"]},
    "rules": [
        {"name": "cluster-name", "literal": "prod-eu-1", "action": "pseudonym"},
        {"name": "project", "pattern": "my-proj-[0-9]+"},
    ],
}
_OAUTH_TOKEN = "ya29." + "A" * 195
# Joined to its URL at runtime rather than written into it: GitHub secret scanning
# reads a whole `postgres://user:password@host/db` literal as a leaked connection
# string, and the redactor's URL rule does not depend on this placeholder's value.
_URL_PASSWORD = "hunter2-example-pw"
# Strict base64 that a text rule would still rewrite (the GCP API-key shape),
# so a test can tell a kept payload from a walked one.
_RULE_MATCHING_BASE64 = "iVBORw0KGgoAIza" + "B" * 37  # a PNG header, then the GCP API-key shape
# A small real PNG start, for parts whose payload only has to be a PNG.
_PNG_BASE64 = "iVBORw0KGgo="
_PDF_BASE64 = "JVBERi0xLjQK"  # %PDF-1.4
_WAV_BASE64 = "UklGRiQAAABXQVZFZm10IA=="  # RIFF....WAVE
# Nesting depths, against the interpreter's recursion limit (1000 by
# default). The copy and walk take about two frames a level, so they stop near
# half the limit and twice it is well past; a hundred times it is past what
# the JSON decoder takes on a default stack; and at a quarter of it arguments
# were walked before the deep-nesting refusal existed and still are.
_TOO_DEEP_TO_WALK = 2 * sys.getrecursionlimit()
_TOO_DEEP_TO_DECODE = 100 * sys.getrecursionlimit()
_DEEP_BUT_WALKABLE = sys.getrecursionlimit() // 4


def _stub_litellm() -> None:
    """Give the callback the one LiteLLM symbol it imports when LiteLLM is absent."""
    # Already stubbed, or really installed: find_spec raises on the stub, so
    # sys.modules is checked first.
    if "litellm" in sys.modules or importlib.util.find_spec("litellm") is not None:
        return

    class CustomLogger:  # the real base class has an __init__ that takes no arguments
        def __init__(self, *args, **kwargs) -> None:
            pass

    package = types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")
    custom_logger.CustomLogger = CustomLogger
    package.integrations = integrations
    integrations.custom_logger = custom_logger
    sys.modules.setdefault("litellm", package)
    sys.modules.setdefault("litellm.integrations", integrations)
    sys.modules.setdefault("litellm.integrations.custom_logger", custom_logger)


def _configmap_mounts(container: dict) -> dict:
    """subPath -> mountPath for the container's mounts of the ConfigMap volume."""
    return {
        m["subPath"]: m["mountPath"]
        for m in container["volumeMounts"]
        if m["name"] == _CONFIG_VOLUME
    }


def _load_callback_module(config: dict):
    """Import the chart's callback the way LiteLLM does: by file path, with the env set."""
    _stub_litellm()
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
        yaml.safe_dump(config, handle)
        config_path = handle.name
    previous = os.environ.get(_CONFIG_ENV_VAR)
    os.environ[_CONFIG_ENV_VAR] = config_path
    try:
        spec = importlib.util.spec_from_file_location("litellm_redaction_callback", _CALLBACK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            os.environ.pop(_CONFIG_ENV_VAR, None)
        else:
            os.environ[_CONFIG_ENV_VAR] = previous
        os.unlink(config_path)


class TestChartCopyParity(unittest.TestCase):
    def test_the_chart_copy_is_byte_identical_to_the_plugin_module(self) -> None:
        self.assertEqual(
            _CHART_REDACTOR.read_bytes(),
            _PLUGIN_REDACTOR.read_bytes(),
            f"{_CHART_REDACTOR.relative_to(_REPO_ROOT)} has drifted from "
            f"{_PLUGIN_REDACTOR.relative_to(_REPO_ROOT)}; edit the plugin copy and "
            f"copy it over, the chart mounts this one into the gateway",
        )

    def test_the_callback_names_the_instance_the_chart_wires(self) -> None:
        module_name, attribute = _CALLBACK_INSTANCE.split(".")
        self.assertEqual(_CALLBACK.name, f"{module_name}.py")
        self.assertIn(f"\n{attribute} = ", _CALLBACK.read_text())


class TestCallback(unittest.TestCase):
    def setUp(self) -> None:
        self._previous_salt = os.environ.get(_SALT_ENV_VAR)
        os.environ[_SALT_ENV_VAR] = "test-salt"
        self.module = _load_callback_module(_RULES_CONFIG)
        self.hook = self.module.proxy_handler_instance

    def tearDown(self) -> None:
        if self._previous_salt is None:
            os.environ.pop(_SALT_ENV_VAR, None)
        else:
            os.environ[_SALT_ENV_VAR] = self._previous_salt

    def _run(self, data: dict, call_type: str = "completion") -> dict:
        return asyncio.run(self.hook.async_pre_call_hook({}, None, data, call_type))

    def test_string_message_content_is_redacted_before_the_provider_call(self) -> None:
        data = {
            "model": "model-default",
            "messages": [
                {"role": "system", "content": "you run on prod-eu-1"},
                {"role": "user", "content": f"token {_OAUTH_TOKEN} pod 10.0.0.5 project my-proj-42"},
                {"role": "tool", "tool_call_id": "x", "content": "127.0.0.1 is loopback"},
            ],
        }
        result = self._run(data)
        self.assertIs(result, data, "the hook returns the request it rewrote in place")
        system, user, tool = (m["content"] for m in result["messages"])
        self.assertRegex(system, r"^you run on \[cluster-name:[0-9a-f]{12}\]$")
        self.assertRegex(
            user, r"^token \[REDACTED_SECRET\] pod \[ip:[0-9a-f]{12}\] project \[REDACTED_PROJECT\]$"
        )
        # The allowlisted loopback address stays, so the model still reads it.
        self.assertEqual(tool, "127.0.0.1 is loopback")
        self.assertEqual(result["model"], "model-default")

    def test_text_content_parts_are_redacted_and_other_parts_left_alone(self) -> None:
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _PNG_BASE64}}
        data = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "node 10.0.0.9"}, image]}
            ]
        }
        parts = self._run(data)["messages"][0]["content"]
        self.assertRegex(parts[0]["text"], r"^node \[ip:[0-9a-f]{12}\]$")
        self.assertEqual(parts[1], image)

    def test_tool_call_arguments_are_redacted_and_stay_valid_json(self) -> None:
        arguments = json.dumps(
            {
                "command": f"curl -H 'Authorization: Bearer {_OAUTH_TOKEN}' http://10.0.0.5/",
                "env": ["DB_URL=postgres://app:" + _URL_PASSWORD + "@db.internal/x"],
                "timeout": 30,
            }
        )
        untouched = json.dumps({"command": "kubectl get pods -n shop"})
        data = {
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "a", "type": "function", "function": {"name": "t", "arguments": arguments}},
                        {"id": "b", "type": "function", "function": {"name": "t", "arguments": untouched}},
                    ],
                }
            ]
        }
        with self.assertLogs(self.module.logger, level="INFO") as captured:
            calls = self._run(data)["messages"][0]["tool_calls"]
        redacted = json.loads(calls[0]["function"]["arguments"])
        self.assertRegex(redacted["command"], r"Bearer \[REDACTED_SECRET\]' http://\[ip:[0-9a-f]{12}\]/$")
        self.assertEqual(redacted["env"], ["DB_URL=postgres://app:[REDACTED_SECRET]@db.internal/x"])
        self.assertEqual(redacted["timeout"], 30)
        # Arguments with nothing to redact keep their exact bytes.
        self.assertEqual(calls[1]["function"]["arguments"], untouched)
        self.assertIn("credential=2", captured.output[0])
        self.assertIn("ip=1", captured.output[0])

    def test_tool_call_arguments_are_redacted_by_key_name_as_well_as_by_shape(self) -> None:
        # The model copies what it read into its own calls; a key name is often
        # the only thing that says a value is a credential.
        shapes = [
            ({"password": "hunter2-example-pw"}, {"password": "[REDACTED_SECRET]"}),
            (
                {"env": [{"name": "DB_PASSWORD", "value": "hunter2-example-pw"}]},
                {"env": [{"name": "DB_PASSWORD", "value": "[REDACTED_SECRET]"}]},
            ),
            (
                {"object": {"kind": "Secret", "data": {"db-pass": "aHVudGVy"}}},
                {"object": {"kind": "Secret", "data": {"db-pass": "[REDACTED_SECRET]"}}},
            ),
        ]
        for arguments, expected in shapes:
            with self.subTest(arguments=arguments):
                data = {
                    "messages": [
                        {
                            "role": "assistant",
                            "tool_calls": [{"function": {"name": "t", "arguments": json.dumps(arguments)}}],
                        }
                    ]
                }
                call = self._run(data)["messages"][0]["tool_calls"][0]
                self.assertEqual(json.loads(call["function"]["arguments"]), expected)

    def test_tool_call_arguments_are_forwarded_redacted_even_when_the_counts_net_zero(self) -> None:
        # A private-key block that already holds a marker: masking it swaps
        # one marker for another, so the count does not move, but the body
        # must still not go out.
        command = (
            "echo '-----BEGIN RSA PRIVATE KEY-----\n[REDACTED_SECRET]\nMIIEowIBAAKCAQEA\n"
            "-----END RSA PRIVATE KEY-----'"
        )
        data = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "t", "arguments": json.dumps({"cmd": command})}}],
                }
            ]
        }
        call = self._run(data)["messages"][0]["tool_calls"][0]
        self.assertNotIn("MIIEowIBAAKCAQEA", call["function"]["arguments"])

    def test_tool_call_arguments_sent_as_an_object_are_walked(self) -> None:
        data = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "t", "arguments": {"api_key": "abcd1234efgh"}}}],
                }
            ]
        }
        call = self._run(data)["messages"][0]["tool_calls"][0]
        self.assertEqual(call["function"]["arguments"], {"api_key": "[REDACTED_SECRET]"})

    def test_tool_call_arguments_that_are_a_json_string_are_decoded(self) -> None:
        # Arguments that encode a JSON string are walked decoded, so a value
        # after an escaped line break or tab is redacted as on main; clean
        # string arguments keep their exact bytes.
        cases = [json.dumps("kubectl get pods\n10.9.8.7"), json.dumps("hosts:\t10.9.8.7")]
        clean = json.dumps("kubectl get pods\n-n shop")
        data = {
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": str(i), "type": "function", "function": {"name": "t", "arguments": a}}
                        for i, a in enumerate(cases + [clean])
                    ],
                }
            ]
        }
        calls = self._run(data)["messages"][0]["tool_calls"]
        for call in calls[:2]:
            decoded = json.loads(call["function"]["arguments"])
            self.assertNotIn("10.9.8.7", decoded)
            self.assertRegex(decoded, r"\[ip:[0-9a-f]{12}\]$")
        self.assertEqual(calls[2]["function"]["arguments"], clean)

    def test_tool_call_arguments_that_are_not_json_are_redacted_as_text(self) -> None:
        data = {
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [{"function": {"name": "t", "arguments": "{truncated 10.0.0.5"}}],
                }
            ]
        }
        call = self._run(data)["messages"][0]["tool_calls"][0]
        self.assertRegex(call["function"]["arguments"], r"^\{truncated \[ip:[0-9a-f]{12}\]$")

    def test_embeddings_input_is_redacted_as_a_string_and_as_a_list(self) -> None:
        single = self._run({"input": "reach 10.0.0.5"}, "embeddings")
        self.assertRegex(single["input"], r"^reach \[ip:[0-9a-f]{12}\]$")
        many = self._run({"input": ["reach 10.0.0.5", "plain text"]}, "embeddings")
        self.assertRegex(many["input"][0], r"^reach \[ip:[0-9a-f]{12}\]$")
        self.assertEqual(many["input"][1], "plain text")

    def test_the_same_identifier_gets_the_same_pseudonym_across_requests(self) -> None:
        first = self._run({"messages": [{"role": "user", "content": "10.0.0.5"}]})
        second = self._run({"messages": [{"role": "user", "content": "10.0.0.5"}]})
        self.assertEqual(first["messages"][0]["content"], second["messages"][0]["content"])

    def test_counts_are_reported_by_rule_name_and_never_the_payload(self) -> None:
        data = {"messages": [{"role": "user", "content": f"{_OAUTH_TOKEN} 10.0.0.5 prod-eu-1"}]}
        with self.assertLogs(self.module.logger, level="INFO") as captured:
            self._run(data)
        self.assertEqual(len(captured.output), 1)
        line = captured.output[0]
        for expected in ("credential=1", "ip=1", "cluster-name=1"):
            self.assertIn(expected, line)
        for literal in ("ya29", "10.0.0.5", "prod-eu-1"):
            self.assertNotIn(literal, line)

    def test_a_request_with_nothing_to_redact_is_returned_unchanged_and_unlogged(self) -> None:
        data = {"messages": [{"role": "user", "content": "list the pods"}], "input": "x"}
        with self.assertRaises(AssertionError):
            with self.assertLogs(self.module.logger, level="INFO"):
                self._run(dict(data))
        self.assertEqual(self._run(data), data)

    # The Anthropic messages shape (/v1/messages), as a coding harness sends it:
    # the system text at the top level, and content blocks that carry text
    # under keys other than `text`.

    def test_anthropic_system_text_is_redacted_as_a_string_and_as_blocks(self) -> None:
        as_string = self._run({"system": "you run on prod-eu-1", "messages": []}, "anthropic_messages")
        self.assertRegex(as_string["system"], r"^you run on \[cluster-name:[0-9a-f]{12}\]$")
        as_blocks = self._run(
            {
                "system": [{"type": "text", "text": "project my-proj-7", "cache_control": {"type": "ephemeral"}}],
                "messages": [],
            },
            "anthropic_messages",
        )
        block = as_blocks["system"][0]
        self.assertEqual(block["text"], "project [REDACTED_PROJECT]")
        self.assertEqual(block["cache_control"], {"type": "ephemeral"})

    def test_anthropic_tool_results_and_tool_inputs_are_redacted_and_thinking_kept(self) -> None:
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG_BASE64}}
        opaque = {"type": "redacted_thinking", "data": "opaque-blob"}
        data = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "the node is 10.0.0.9", "signature": "sig"},
                        opaque,
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"command": f"curl -H 'Authorization: Bearer {_OAUTH_TOKEN}' http://10.0.0.5/"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t1", "content": "pod ip 10.0.0.5"},
                        {
                            "type": "tool_result",
                            "tool_use_id": "t2",
                            "content": [{"type": "text", "text": f"token {_OAUTH_TOKEN}"}, image],
                        },
                    ],
                },
            ]
        }
        result = self._run(data, "anthropic_messages")
        thinking, kept, tool_use = result["messages"][0]["content"]
        # Signed thinking is left as the provider issued it: the provider
        # checks the text against the signature, and the model wrote it from
        # input the hook had already redacted.
        self.assertEqual(thinking["thinking"], "the node is 10.0.0.9")
        self.assertEqual(thinking["signature"], "sig")
        self.assertEqual(kept, opaque)
        self.assertNotIn(_OAUTH_TOKEN, json.dumps(tool_use["input"]))
        self.assertNotIn("10.0.0.5", json.dumps(tool_use["input"]))
        self.assertEqual(tool_use["name"], "Bash")
        first, second = result["messages"][1]["content"]
        self.assertRegex(first["content"], r"^pod ip \[ip:[0-9a-f]{12}\]$")
        self.assertEqual(second["content"][0]["text"], "token [REDACTED_SECRET]")
        self.assertEqual(second["content"][1], image)

    def test_a_tool_input_sent_as_a_json_string_stays_a_json_string(self) -> None:
        data = {
            "messages": [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "x", "input": json.dumps({"host": "10.0.0.5"})}]}
            ]
        }
        sent = self._run(data, "anthropic_messages")["messages"][0]["content"][0]["input"]
        self.assertIsInstance(sent, str)
        self.assertNotIn("10.0.0.5", sent)
        json.loads(sent)

    def test_a_part_type_this_file_does_not_know_is_walked_whole(self) -> None:
        data = {"messages": [{"role": "user", "content": [{"type": "input_text", "text": "on prod-eu-1"}, {"type": "search_result", "source": "x", "content": [{"type": "text", "text": "my-proj-3"}]}]}]}
        parts = self._run(data)["messages"][0]["content"]
        self.assertNotIn("prod-eu-1", json.dumps(parts))
        self.assertNotIn("my-proj-3", json.dumps(parts))
        self.assertEqual(parts[1]["type"], "search_result")

    # The Gemini shape (/v1beta/models/<model>:generateContent), as the
    # Antigravity CLI sends it.

    def test_gemini_contents_function_calls_and_responses_are_redacted(self) -> None:
        inline = {"inlineData": {"mimeType": "image/png", "data": _PNG_BASE64}}
        data = {
            "contents": [
                {"role": "user", "parts": [{"text": "pods on prod-eu-1"}, inline]},
                {
                    "role": "model",
                    "parts": [
                        {"text": "checking 10.0.0.9", "thought": True},
                        {"functionCall": {"name": "run_command", "args": {"command": f"export TOKEN={_OAUTH_TOKEN}"}}, "thoughtSignature": _RULE_MATCHING_BASE64},
                    ],
                },
                {"role": "user", "parts": [{"functionResponse": {"name": "run_command", "response": {"output": "project my-proj-12 at 10.0.0.5"}}}]},
            ],
            "generationConfig": {"temperature": 0},
        }
        result = self._run(data, "agenerate_content")
        user, model, response = result["contents"]
        self.assertRegex(user["parts"][0]["text"], r"^pods on \[cluster-name:[0-9a-f]{12}\]$")
        self.assertEqual(user["parts"][1], inline)
        self.assertRegex(model["parts"][0]["text"], r"^checking \[ip:[0-9a-f]{12}\]$")
        self.assertTrue(model["parts"][0]["thought"])
        call = model["parts"][1]
        self.assertNotIn(_OAUTH_TOKEN, json.dumps(call["functionCall"]["args"]))
        self.assertEqual(call["thoughtSignature"], _RULE_MATCHING_BASE64)
        output = json.dumps(response["parts"][0]["functionResponse"]["response"])
        self.assertNotIn("my-proj-12", output)
        self.assertNotIn("10.0.0.5", output)
        self.assertEqual(result["generationConfig"], {"temperature": 0})

    def test_gemini_system_instruction_is_redacted_under_either_spelling(self) -> None:
        for key in ("systemInstruction", "system_instruction"):
            data = {key: {"parts": [{"text": "you run on prod-eu-1"}]}, "contents": []}
            text = self._run(data, "agenerate_content_stream")[key]["parts"][0]["text"]
            self.assertRegex(text, r"^you run on \[cluster-name:[0-9a-f]{12}\]$", key)

    # Payloads and controls: only a part's binary payload and the request's
    # control keys are left alone.

    def test_only_a_parts_binary_payload_is_skipped(self) -> None:
        data = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        # A payload a text rule would rewrite if it were walked.
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _RULE_MATCHING_BASE64}, "title": "node 10.0.0.9"},
                        {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "on prod-eu-1"}, "context": "my-proj-4"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _PNG_BASE64}},
                        {"type": "image_url", "image_url": {"url": f"https://example.com/x?token={_OAUTH_TOKEN}"}},
                    ],
                }
            ]
        }
        image, document, inline, linked = self._run(data, "anthropic_messages")["messages"][0]["content"]
        self.assertEqual(image["source"]["data"], _RULE_MATCHING_BASE64)
        self.assertNotIn("10.0.0.9", image["title"])
        self.assertNotIn("prod-eu-1", json.dumps(document))
        self.assertNotIn("my-proj-4", json.dumps(document))
        self.assertEqual(inline["image_url"]["url"], "data:image/png;base64," + _PNG_BASE64)
        self.assertNotIn(_OAUTH_TOKEN, json.dumps(linked))

    def test_gemini_binary_inline_data_keeps_its_payload_and_text_inline_data_is_refused(self) -> None:
        binary = {"contents": [{"role": "user", "parts": [{"inlineData": {"mimeType": "image/png", "data": _RULE_MATCHING_BASE64}}]}]}
        self.assertEqual(self._run(binary, "agenerate_content")["contents"][0]["parts"][0]["inlineData"]["data"], _RULE_MATCHING_BASE64)
        for mime in ("text/plain", "application/json"):
            text = {"contents": [{"role": "user", "parts": [{"inlineData": {"mimeType": mime, "data": "b24gcHJvZC1ldS0x"}}]}]}
            with self.assertRaises(HTTPException, msg=mime):
                self._run(text, "agenerate_content")

    def test_a_gemini_function_responses_own_parts_keep_binary_and_refuse_text(self) -> None:
        for key in ("functionResponse", "function_response"):
            for inline in ("inlineData", "inline_data"):
                with self.subTest(key=key, inline=inline):
                    binary = {"contents": [{"role": "user", "parts": [{key: {
                        "name": "shot", "response": {"note": "pod 10.0.0.9"},
                        "parts": [{inline: {"mimeType": "image/png", "data": _RULE_MATCHING_BASE64}}],
                    }}]}]}
                    out = self._run(binary, "agenerate_content")["contents"][0]["parts"][0][key]
                    self.assertEqual(out["parts"][0][inline]["data"], _RULE_MATCHING_BASE64)
                    # The response object beside the parts is still walked.
                    self.assertNotIn("10.0.0.9", json.dumps(out["response"]))
                    text = {"contents": [{"role": "user", "parts": [{key: {
                        "name": "shot", "parts": [{inline: {"mimeType": "text/plain", "data": "b24gcHJvZC1ldS0x"}}],
                    }}]}]}
                    with self.assertRaises(HTTPException) as refused:
                        self._run(text, "agenerate_content")
                    self.assertEqual(refused.exception.status_code, 400)

    def test_responses_input_parts_keep_binary_payloads_and_refuse_text_ones(self) -> None:
        image_url = "data:image/png;base64," + _RULE_MATCHING_BASE64
        file_data = "data:application/pdf;base64," + _PDF_BASE64
        data = {"input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "on prod-eu-1"},
            {"type": "input_image", "image_url": image_url},
            {"type": "input_file", "filename": "r.pdf", "file_data": file_data},
        ]}]}
        text, image, filed = self._run(data, "aresponses")["input"][0]["content"]
        self.assertNotIn("prod-eu-1", text["text"])
        self.assertEqual(image["image_url"], image_url)
        self.assertEqual(filed["file_data"], file_data)
        refused_parts = [
            {"type": "input_image", "image_url": "data:image/png;base64,b24gcHJvZC1ldS0x"},
            {"type": "input_file", "file_data": "data:application/json;base64,e30="},
            {"type": "input_file", "file_data": "b24gcHJvZC1ldS0x"},
        ]
        for part in refused_parts:
            with self.subTest(part=part), self.assertRaises(HTTPException) as refused:
                self._run({"input": [{"type": "message", "role": "user", "content": [part]}]}, "aresponses")
            self.assertEqual(refused.exception.status_code, 400)

    def test_a_responses_function_call_outputs_parts_keep_binary_and_refuse_text(self) -> None:
        image_url = "data:image/png;base64," + _RULE_MATCHING_BASE64
        data = {"input": [{"type": "function_call_output", "call_id": "c", "output": [
            {"type": "input_text", "text": "on prod-eu-1"},
            {"type": "input_image", "image_url": image_url},
        ]}]}
        text, image = self._run(data, "aresponses")["input"][0]["output"]
        self.assertNotIn("prod-eu-1", text["text"])
        self.assertEqual(image["image_url"], image_url)
        refused = {"input": [{"type": "function_call_output", "call_id": "c", "output": [
            {"type": "input_file", "file_data": "data:text/plain;base64,cHJvZC1ldS0x"},
        ]}]}
        with self.assertRaises(HTTPException) as caught:
            self._run(refused, "aresponses")
        self.assertEqual(caught.exception.status_code, 400)
        # A string output is still walked as text.
        walked = self._run({"input": [{"type": "function_call_output", "call_id": "c", "output": "on prod-eu-1"}]}, "aresponses")
        self.assertNotIn("prod-eu-1", walked["input"][0]["output"])

    def test_a_payload_takes_whitespace_only_as_line_wraps(self) -> None:
        import base64 as b64
        png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2
        flat = b64.b64encode(png).decode()
        wrapped = "\n".join(flat[i:i + 76] for i in range(0, len(flat), 76))
        crlf = "\r\n".join(flat[i:i + 64] for i in range(0, len(flat), 64)) + "\r\n"
        for data in (flat, wrapped, crlf):
            with self.subTest(data=data[:20]):
                for req, get, call in self._payload_positions(data):
                    self.assertEqual(get(self._run(req, call)), data)
        # Words in the base64 alphabet after a real header, with any other
        # whitespace: refused at every position, never kept as they came.
        smuggled = [
            "iVBORw0KGgo the cluster is prod eu one",
            "iVBORw0KGgo\tAAAA",
            "iVBORw0KGgo=\nAAAA\nAAAAAAAA",
            flat[:76] + "\n" + flat[76:100] + "\n" + flat[100:176],
        ]
        for data in smuggled:
            for req, _, call in self._payload_positions(data):
                with self.subTest(data=data[:24], call=call), self.assertRaises(HTTPException) as refused:
                    self._run(req, call)
                self.assertEqual(refused.exception.status_code, 400)

    @staticmethod
    def _payload_positions(data: str):
        url = "data:image/png;base64," + data
        return [
            ({"contents": [{"role": "user", "parts": [{"inlineData": {"mimeType": "image/png", "data": data}}]}]},
             lambda r: r["contents"][0]["parts"][0]["inlineData"]["data"], "agenerate_content"),
            ({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]},
             lambda r: r["messages"][0]["content"][0]["image_url"]["url"][len("data:image/png;base64,"):], "completion"),
            ({"input": [{"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": url}]}]},
             lambda r: r["input"][0]["content"][0]["image_url"][len("data:image/png;base64,"):], "aresponses"),
        ]

    # A value only a key-name rule masks: the text rules don't know "auth".
    _KEYED = '{"auth":"opaque-value-xyz","name":"web"}'
    _KEYED_OUT = '{"auth":"[REDACTED_SECRET]","name":"web"}'

    def test_json_tool_output_gets_the_structure_rules_at_each_position(self) -> None:
        cases = [
            ({"messages": [{"role": "tool", "tool_call_id": "x", "content": self._KEYED}]},
             lambda r: r["messages"][0]["content"], "completion"),
            ({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": self._KEYED}]}]},
             lambda r: r["messages"][0]["content"][0]["content"], "anthropic_messages"),
            ({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x",
                                                         "content": [{"type": "text", "text": self._KEYED}]}]}]},
             lambda r: r["messages"][0]["content"][0]["content"][0]["text"], "anthropic_messages"),
            ({"input": [{"type": "function_call_output", "call_id": "c", "output": self._KEYED}]},
             lambda r: r["input"][0]["output"], "aresponses"),
            ({"contents": [{"role": "user", "parts": [{"functionResponse": {"name": "t", "response": {"output": self._KEYED}}}]}]},
             lambda r: r["contents"][0]["parts"][0]["functionResponse"]["response"]["output"], "agenerate_content"),
            ({"contents": [{"role": "user", "parts": [{"function_response": {"name": "t", "response": {"output": self._KEYED}}}]}]},
             lambda r: r["contents"][0]["parts"][0]["function_response"]["response"]["output"], "agenerate_content"),
        ]
        for req, get, call in cases:
            with self.subTest(call=call, req=json.dumps(req)[:60]):
                self.assertEqual(get(self._run(req, call)), self._KEYED_OUT)

    def test_a_changed_tool_output_keeps_its_layout_and_an_unchanged_one_its_bytes(self) -> None:
        secret = {"kind": "Secret", "metadata": {"name": "s"}, "data": {"k": "c2VjcmV0"}}
        for text in (json.dumps(secret, indent=2) + "\n", json.dumps(secret, indent=4), json.dumps(secret, separators=(",", ":")),
                     json.dumps(secret, indent="\t")):
            with self.subTest(text=text[:30]):
                out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]})["messages"][0]["content"]
                want = json.loads(text)
                want["data"]["k"] = "[REDACTED_SECRET]"
                indent = None if "\n" not in text.strip() else ("\t" if "\t" in text else len(text.split("\n")[1]) - len(text.split("\n")[1].lstrip()))
                separators = (",", ": ") if indent is not None else (",", ":")
                self.assertEqual(out, json.dumps(want, indent=indent, separators=separators) + ("\n" if text.endswith("\n") else ""))
        odd = '{"a": "b",   "c": 1}'
        out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": odd}]})["messages"][0]["content"]
        self.assertEqual(out, odd)

    def test_json_inside_a_decoded_tool_output_is_decoded_too(self) -> None:
        # The nested value only a key-name rule masks: the text rules leave it,
        # so only the nested decoding catches it, and the changed inner
        # document keeps its indent.
        stdout = json.dumps({"auth": "opaque-value-xyz", "name": "web"}, indent=2)
        output = json.dumps({"exit_code": 0, "stdout": stdout, "stderr": ""})
        req = {"contents": [{"role": "user", "parts": [{"functionResponse": {"name": "bash", "response": {"output": output}}}]}]}
        out = json.loads(self._run(req, "agenerate_content")["contents"][0]["parts"][0]["functionResponse"]["response"]["output"])
        self.assertEqual(out["exit_code"], 0)
        self.assertEqual(json.loads(out["stdout"])["auth"], "[REDACTED_SECRET]")
        self.assertTrue(out["stdout"].startswith('{\n  "auth"'), "the inner document keeps its indent")

    def test_a_gemini_response_secret_with_a_json_data_string_is_masked_whole(self) -> None:
        response = {"kind": "Secret", "data": json.dumps({"cfg": json.dumps({"v": "opaque-zz"})})}
        req = {"contents": [{"role": "user", "parts": [{"functionResponse": {"name": "t", "response": response}}]}]}
        out = self._run(req, "agenerate_content")
        self.assertNotIn("opaque-zz", json.dumps(out))

    def test_a_bare_pseudonym_marker_still_lets_the_output_decode(self) -> None:
        config = dict(_RULES_CONFIG, rules=_RULES_CONFIG["rules"] + [{"name": "acct", "pattern": "4[0-9]{8}", "action": "pseudonym"}])
        hook = _load_callback_module(config).proxy_handler_instance
        text = '{"account": 412345678, "auth": "opaque-value-xyz"}'
        out = asyncio.run(hook.async_pre_call_hook({}, None, {"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]}, "completion"))
        content = out["messages"][0]["content"]
        self.assertNotIn("opaque-value-xyz", content)
        self.assertNotIn("412345678", content)
        json.loads(content)

    def test_a_number_a_second_text_pass_masks_stays_valid_json(self) -> None:
        text = '{"token":\n  123456789,\n  "auth": "opaque-value-xyz"}'
        out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]})["messages"][0]["content"]
        self.assertNotIn("123456789", out)
        self.assertNotIn("opaque-value-xyz", out)
        json.loads(out)

    def test_tool_output_past_the_bounds_or_not_whole_json_is_matched_as_text(self) -> None:
        big = json.dumps({"auth": "opaque-value-xyz", "pad": "x" * (256 * 1024)})
        deep = '{"auth":"opaque-value-xyz","d":' + "[" * 64 + "1" + "]" * 64 + "}"
        prefixed = "result: " + self._KEYED
        for text in (big, deep, prefixed, "[1, 2"):
            with self.subTest(text=text[:40]):
                out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]})["messages"][0]["content"]
                self.assertEqual(out, text, "left to the text rules, which don't mask a value only its key names")
        # Two levels under the bound is decoded.
        ok = '{"auth":"opaque-value-xyz","d":' + "[" * 62 + "1" + "]" * 62 + "}"
        out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": ok}]})["messages"][0]["content"]
        self.assertIn("[REDACTED_SECRET]", out)

    def test_decoding_tool_output_never_masks_less_than_the_text_rules_did(self) -> None:
        # A JSON string under a credential key, and a number there, are masked
        # whole as before decoding; decoding must not open them up.
        cases = [
            ('{"token": "{\\"v\\":\\"opaque-zz\\"}"}', "opaque-zz"),
            ('{"token": 123456789, "n":"x"}', "123456789"),
            ('{"password": 98765432}', "98765432"),
            ('{"outer": {"token": "[\\"opaque-zz\\"]"}}', "opaque-zz"),
        ]
        for text, secret in cases:
            with self.subTest(text=text):
                out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]})["messages"][0]["content"]
                self.assertNotIn(secret, out)
        # The same at the Gemini response level, where the response is already
        # a mapping: a JSON string under a credential key stays a string and is
        # masked whole.
        for key in ("token", "password", "apiKey"):
            with self.subTest(key=key):
                req = {"contents": [{"role": "user", "parts": [{"functionResponse": {"name": "t", "response": {key: '{"v":"opaque-zz"}'}}}]}]}
                out = self._run(req, "agenerate_content")["contents"][0]["parts"][0]["functionResponse"]["response"]
                self.assertNotIn("opaque-zz", json.dumps(out))
        # A Secret's data value that is itself JSON is masked whole, not opened.
        secret = json.dumps({"kind": "Secret", "data": {"cfg": '{"v":"opaque-zz"}'}})
        out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": secret}]})["messages"][0]["content"]
        self.assertNotIn("opaque-zz", out)

    def test_a_tool_output_whose_escapes_end_a_value_stays_text(self) -> None:
        # The text rules mask a bare value through a \\u escape; decoded, the
        # escape would end it. Such output keeps the text rules' result.
        for escape in ("\\u0020", "\\u0022", "\\u002c", "\\u007d", "\\u00a0", "\\u2028"):
            with self.subTest(escape=escape):
                text = '{"cmd":"mysql --password=abc' + escape + 'opaqueBBB"}'
                out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]})["messages"][0]["content"]
                self.assertNotIn("opaqueBBB", out)
        # An escape for an ordinary character (Go writes < > & this way) still
        # lets the output be decoded.
        text = '{"auth":"opaque-value-xyz","html":"\\u003cb\\u003e"}'
        out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]})["messages"][0]["content"]
        self.assertNotIn("opaque-value-xyz", out)

    def test_escapes_and_split_values_in_tool_output_stay_masked(self) -> None:
        # The text rules run over the output as it came, before it is decoded,
        # so a match that needs the escaped form (a URL password through an
        # escape, a value through \\f, a key id before an escaped letter, a
        # private key across strings) masks what it masked before.
        cases = [
            ('{"u":"postgres://app:s3cr\\u003cetzz@10.0.0.5:5432/app"}', "etzz"),
            ('{"u":"redis://default:ab\\/cdzz@cache:6379","e":"bob@example.com"}', "cdzz"),
            ('{"u": "postgres://admin:hunter2\\"zzqqsecret@db.internal.example.com/x", "e": "bob@example.com"}', "hunter2"),
            ('{"log":"API_TOKEN=abcd1234\\fwxyz9876"}', "wxyz9876"),
            ('{"k": "AKIAIOSFODNN7EXAMPL5\\u005f", "e": "bob@example.com"}', "AKIAIOSFODNN7EXAMPL5"),
            ('{"stdout_lines": ["secret=-----BEGIN RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEAbodybodybody", "-----END RSA PRIVATE KEY-----"]}', "bodybodybody"),
            ('{"cmd":"mysql --password=abc\\u0020opaqueBBB"}', "opaqueBBB"),
        ]
        for text, secret in cases:
            for req, get, call in (
                ({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]}, lambda r: r["messages"][0]["content"], "completion"),
                ({"contents": [{"role": "user", "parts": [{"functionResponse": {"name": "t", "response": {"output": text}}}]}]},
                 lambda r: json.dumps(r["contents"][0]["parts"][0]["functionResponse"]["response"]), "agenerate_content"),
            ):
                with self.subTest(text=text[:40], call=call):
                    self.assertNotIn(secret, get(self._run(req, call)))

    def test_markers_and_plain_strings_do_not_use_the_nested_budget(self) -> None:
        # Forty IPs become forty [ip:...] markers before decoding; the nested
        # shell result is still decoded.
        # The result comes first, so the walk reaches the markers before it.
        doc = {"result": {"stdout": self._KEYED}, "pods": ["10.0.0.%d" % i for i in range(1, 41)],
               "logs": ["[INFO] line %d" % i for i in range(40)]}
        out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": json.dumps(doc)}]})["messages"][0]["content"]
        self.assertNotIn("opaque-value-xyz", out)

    def test_json_nested_under_a_secrets_data_string_is_masked_whole(self) -> None:
        inner = json.dumps({"cfg": json.dumps({"v": "opaque-zz"})})
        text = json.dumps({"kind": "Secret", "data": inner})
        out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]})["messages"][0]["content"]
        self.assertNotIn("opaque-zz", out)

    def test_a_number_under_a_credential_key_leaves_the_rest_to_the_structure_rules(self) -> None:
        text = '{"token": 123456789, "auth": "opaque-value-xyz", "n": [1, 2]}'
        out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]})["messages"][0]["content"]
        self.assertNotIn("123456789", out)
        self.assertNotIn("opaque-value-xyz", out)
        json.loads(out)

    def test_an_operator_rule_matching_the_probe_word_does_not_stop_decoding(self) -> None:
        config = dict(_RULES_CONFIG, rules=_RULES_CONFIG["rules"] + [{"name": "ours", "literal": "kube-agents"}])
        hook = _load_callback_module(config).proxy_handler_instance
        output = json.dumps({"exit_code": 0, "stdout": self._KEYED})
        req = {"contents": [{"role": "user", "parts": [{"functionResponse": {"name": "bash", "response": {"output": output}}}]}]}
        out = asyncio.run(hook.async_pre_call_hook({}, None, req, "agenerate_content"))
        self.assertNotIn("opaque-value-xyz", json.dumps(out))

    def test_a_tool_output_with_a_repeated_key_gets_the_text_rules_only(self) -> None:
        # The decoder would keep one value; what the text says is what counts.
        text = '{"kind":"Secret","kind":"Other","data":{"k":"opaque-value-xyz"}}'
        out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]})["messages"][0]["content"]
        self.assertNotIn("opaque-value-xyz", out)
        self.assertIn('"kind":"Other"', out, "left as text, both keys kept")

    def test_a_changed_tool_output_stays_valid_json_whatever_whitespace_its_strings_hold(self) -> None:
        for text in ('["nicole,\u00a0x", {"password":"opaque-abc"}, 1]', '{"a\u2028":"b", "password":"opaque-abc"}'):
            with self.subTest(text=text):
                out = self._run({"messages": [{"role": "tool", "tool_call_id": "x", "content": text}]})["messages"][0]["content"]
                json.loads(out)
                self.assertNotIn("opaque-abc", out)

    def test_payloads_in_url_safe_or_unpadded_base64_and_image_jpg_are_kept(self) -> None:
        # Forms providers accept: the URL-safe alphabet, no padding, and the
        # common image/jpg label. Each is kept byte for byte as it came.
        import base64 as b64
        png = b"\x89PNG\r\n\x1a\n" + bytes([0xFB, 0xFF, 0xFE]) * 4
        jpeg = b"\xff\xd8\xff\xe0" + bytes([0xFB, 0xFF]) * 4
        cases = [
            ("image/png", b64.urlsafe_b64encode(png).decode()),
            ("image/png", b64.b64encode(png).decode().rstrip("=")),
            ("image/png", b64.urlsafe_b64encode(png).decode().rstrip("=")),
            ("image/jpg", b64.b64encode(jpeg).decode()),
        ]
        for mime, data in cases:
            with self.subTest(mime=mime, data=data):
                req = {"contents": [{"role": "user", "parts": [{"inlineData": {"mimeType": mime, "data": data}}]}]}
                out = self._run(req, "agenerate_content")
                self.assertEqual(out["contents"][0]["parts"][0]["inlineData"]["data"], data)
        # Normalising doesn't loosen the decode: a length that is 1 mod 4, an
        # '=' inside the data, and over-padding are still not base64.
        for bad in ("iVBORw0KGgoAA", "iVBO=Rw0KGgo", "iVBORw0KGgo=AAA", "iVBORw0KGgo==="):
            with self.subTest(bad=bad), self.assertRaises(HTTPException) as refused:
                self._run({"contents": [{"role": "user", "parts": [{"inlineData": {"mimeType": "image/png", "data": bad}}]}]}, "agenerate_content")
            self.assertEqual(refused.exception.status_code, 400)
        # A URL-safe payload that isn't the media it claims is still refused.
        text = b64.urlsafe_b64encode(b"on prod-eu-1, token here").decode().rstrip("=")
        with self.assertRaises(HTTPException):
            self._run({"contents": [{"role": "user", "parts": [{"inlineData": {"mimeType": "image/png", "data": text}}]}]}, "agenerate_content")

    # Provider-opaque fields a provider checks byte for byte: kept as they came
    # at their API positions, even when a rule matches inside them, and
    # redacted anywhere else.
    _OPAQUE = "blob-prod-eu-1-my-proj-12-AbCd"

    def test_a_responses_reasoning_items_encrypted_content_is_kept(self) -> None:
        data = {"input": [
            {"type": "reasoning", "id": "r1", "encrypted_content": self._OPAQUE, "summary": []},
            {"type": "message", "role": "user", "content": "on prod-eu-1", "encrypted_content": self._OPAQUE},
        ]}
        out = self._run(data, "aresponses")
        self.assertEqual(out["input"][0]["encrypted_content"], self._OPAQUE)
        self.assertNotIn("prod-eu-1", out["input"][1]["content"])
        self.assertNotEqual(out["input"][1]["encrypted_content"], self._OPAQUE, "only a reasoning item's is opaque")

    def test_an_anthropic_web_search_results_encrypted_content_is_kept_in_the_assistants_turn(self) -> None:
        def block():
            return {"type": "web_search_tool_result", "tool_use_id": "s1", "content": [
                {"type": "web_search_result", "url": "https://example.com", "title": "prod-eu-1", "encrypted_content": self._OPAQUE}]}
        data = {"messages": [
            {"role": "assistant", "content": [block()]},
            {"role": "user", "content": [block()]},
        ]}
        out = self._run(data)
        kept = out["messages"][0]["content"][0]["content"][0]
        self.assertEqual(kept["encrypted_content"], self._OPAQUE)
        self.assertNotIn("prod-eu-1", kept["title"], "only the encrypted content is opaque")
        self.assertNotEqual(out["messages"][1]["content"][0]["content"][0]["encrypted_content"], self._OPAQUE, "not in a user's turn")

    def test_an_assistants_reasoning_details_data_and_signature_are_kept(self) -> None:
        def details():
            return [{"type": "reasoning.encrypted", "data": self._OPAQUE, "signature": self._OPAQUE, "text": "prod-eu-1"}]
        data = {"messages": [
            {"role": "assistant", "content": "ok", "reasoning_details": details()},
            {"role": "user", "content": "hi", "reasoning_details": details()},
        ]}
        out = self._run(data)
        kept = out["messages"][0]["reasoning_details"][0]
        self.assertEqual((kept["data"], kept["signature"]), (self._OPAQUE, self._OPAQUE))
        self.assertNotIn("prod-eu-1", kept["text"], "only data and signature are opaque")
        self.assertNotEqual(out["messages"][1]["reasoning_details"][0]["data"], self._OPAQUE, "not on a user's message")

    def test_the_chat_shapes_thought_signature_is_kept_on_the_message_and_its_tool_calls(self) -> None:
        call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"},
                "provider_specific_fields": {"thought_signature": self._OPAQUE}}
        data = {"messages": [
            {"role": "assistant", "content": "ok", "tool_calls": [call], "provider_specific_fields": {"thought_signature": self._OPAQUE}},
            {"role": "user", "content": "hi", "provider_specific_fields": {"thought_signature": self._OPAQUE}},
        ]}
        out = self._run(data)
        assistant = out["messages"][0]
        self.assertEqual(assistant["provider_specific_fields"]["thought_signature"], self._OPAQUE)
        self.assertEqual(assistant["tool_calls"][0]["provider_specific_fields"]["thought_signature"], self._OPAQUE)
        self.assertNotEqual(out["messages"][1]["provider_specific_fields"]["thought_signature"], self._OPAQUE, "not on a user's message")

    def test_a_refused_request_shape_is_answered_with_400_through_the_hook(self) -> None:
        # Through the hook LiteLLM calls: a refused shape becomes an
        # HTTPException with status 400, carrying the refusal text, so a
        # caller doesn't retry it as it would a 5xx. Other faults stay 500s.
        data = {"messages": "not a list"}
        with self.assertRaises(HTTPException) as refused:
            self._run(data)
        self.assertEqual(refused.exception.status_code, 400)
        self.assertIn("refuses a request it cannot walk", str(refused.exception.detail))

    def test_json_nested_too_deeply_to_walk_is_refused_with_400(self) -> None:
        # Nesting past Python's recursion limit used to escape the hook as a
        # RecursionError, which LiteLLM answers with a 500 that a caller
        # retries. The same request is refused every time, so it is a 400 like
        # any other request the hook cannot walk, and its text is never in
        # the refusal. The shapes: arguments the decoder itself can't take,
        # arguments and an Anthropic tool input it decodes but the copy and
        # walk can't, and object-shaped values the walk can't.
        secret = "prod-eu-1"

        def nested(depth: int) -> str:
            return "[" * depth + json.dumps(secret) + "]" * depth

        def tool_call(arguments) -> dict:
            return {"messages": [{"role": "assistant", "content": None, "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "t", "arguments": arguments}},
            ]}]}

        deep_object: object = secret
        for _ in range(_TOO_DEEP_TO_WALK):
            deep_object = [deep_object]
        cases = {
            "arguments the decoder can't take": tool_call(nested(_TOO_DEEP_TO_DECODE)),
            "arguments the walk can't take": tool_call(nested(_TOO_DEEP_TO_WALK)),
            "an Anthropic tool input the walk can't take": {"messages": [{"role": "assistant", "content": [
                {"type": "tool_use", "id": "u", "name": "t", "input": nested(_TOO_DEEP_TO_WALK)},
            ]}]},
            "arguments sent as an object": tool_call(deep_object),
            "a top-level field": {"messages": [{"role": "user", "content": "hi"}], "tools": deep_object},
        }
        for name, data in cases.items():
            with self.subTest(name):
                with self.assertRaises(HTTPException) as refused:
                    self._run(data)
                self.assertEqual(refused.exception.status_code, 400)
                self.assertIn("nested too deeply", str(refused.exception.detail))
                self.assertNotIn(secret, str(refused.exception.detail))

    def test_deeply_nested_arguments_that_can_be_walked_are_still_redacted(self) -> None:
        # The refusal is for what can't be walked, not a depth cap: arguments
        # nested this deep were redacted before it existed and still are.
        arguments = "[" * _DEEP_BUT_WALKABLE + json.dumps("prod-eu-1") + "]" * _DEEP_BUT_WALKABLE
        data = {"messages": [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "t", "arguments": arguments}},
        ]}]}
        sent = self._run(data)["messages"][0]["tool_calls"][0]["function"]["arguments"]
        self.assertNotIn("prod-eu-1", sent)
        self.assertIn("[cluster-name:", sent)

    def test_a_fault_in_the_hook_is_not_answered_with_400(self) -> None:
        # Only a refused shape becomes a 400: any other exception from the
        # walk surfaces as itself, which LiteLLM answers with a 500.
        from unittest import mock

        with mock.patch.object(type(self.hook), "redact_request", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._run({"messages": [{"role": "user", "content": "x"}]})

    def test_a_refusal_names_a_media_type_only_when_it_is_a_media_type_name(self) -> None:
        def part(media):
            return {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:{media};base64,AAAA"}}]}]}
        with self.assertRaises(HTTPException) as named:
            self._run(part("text/plain"))
        self.assertIn("text/plain", str(named.exception))
        with self.assertRaises(HTTPException) as unnamed:
            self._run(part("deploy key is hunter2-example-pw"))
        self.assertNotIn("hunter2", str(unnamed.exception))

    def test_top_level_text_outside_the_known_shapes_is_redacted_and_controls_are_not(self) -> None:
        tools = [{"name": "login", "description": "logs in to prod-eu-1", "input_schema": {"type": "object", "properties": {"password": {"type": "string"}}}}]
        data = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": tools,
            "instructions": "you run on prod-eu-1",
            "function_call": {"name": "f", "arguments": json.dumps({"host": "10.0.0.5"})},
            "metadata": {"user_api_key_hash": "sk-hash-should-stay", "note": "prod-eu-1"},
            "api_key": f"Bearer {_OAUTH_TOKEN}",
            "litellm_call_id": "prod-eu-1",
            "model": "prod-eu-1",
        }
        result = self._run(data)
        self.assertNotIn("prod-eu-1", result["tools"][0]["description"])
        # Text rules only: a schema's `password` property is still a schema.
        self.assertEqual(result["tools"][0]["input_schema"]["properties"]["password"], {"type": "string"})
        self.assertNotIn("prod-eu-1", result["instructions"])
        self.assertNotIn("10.0.0.5", json.dumps(result["function_call"]))
        # Controls and the gateway's own bookkeeping are left as they came.
        self.assertEqual(result["metadata"], {"user_api_key_hash": "sk-hash-should-stay", "note": "prod-eu-1"})
        self.assertEqual(result["api_key"], f"Bearer {_OAUTH_TOKEN}")
        self.assertEqual(result["litellm_call_id"], "prod-eu-1")
        self.assertEqual(result["model"], "prod-eu-1")

    def test_exempt_looking_shapes_inside_tool_traffic_are_walked(self) -> None:
        # Only the positions the APIs define are exempt. The same shapes inside
        # a tool's input, a tool's output or a function's arguments are data,
        # and walked.
        secret = _RULE_MATCHING_BASE64
        data = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "t", "name": "x", "input": {"type": "thinking", "thinking": f"token {_OAUTH_TOKEN}", "signature": "s"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": f"data:image/png;base64,{secret}"},
                        {"type": "tool_result", "tool_use_id": "u", "content": [{"type": "text", "text": "x", "nested": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": secret}}]}]},
                        # Two levels down is the tool's own output, not a part.
                        {"type": "tool_result", "tool_use_id": "v", "content": [{"type": "document", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": secret}}]}]},
                    ],
                },
            ]
        }
        sent = json.dumps(self._run(data, "anthropic_messages"))
        self.assertNotIn(_OAUTH_TOKEN, sent)
        self.assertNotIn(secret, sent)
        gemini = {
            "contents": [
                {"role": "user", "parts": [{"functionResponse": {"name": "f", "response": {"inlineData": {"mimeType": "image/png", "data": secret}, "thoughtSignature": secret}}}]}
            ]
        }
        self.assertNotIn(secret, json.dumps(self._run(gemini, "agenerate_content")))

    def test_a_payload_position_holding_anything_but_binary_base64_is_refused(self) -> None:
        for part in (
            {"type": "image", "source": {"type": "base64", "media_type": "image/svg+xml", "data": "PHN2Zz4="}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "not base64 at all!"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,not base64!"}},
            {"type": "image_url", "image_url": {"url": "data:image/png,plain"}},
            {"type": "input_audio", "input_audio": {"format": "wav", "data": "not base64!"}},
        ):
            with self.assertRaises(HTTPException, msg=json.dumps(part)):
                self._run({"messages": [{"role": "user", "content": [part]}]})
        with self.assertRaises(HTTPException):
            self._run({"contents": [{"role": "user", "parts": [{"inlineData": {"mimeType": "image/svg+xml", "data": "PHN2Zz4="}}]}]}, "agenerate_content")

    def test_thinking_is_exempt_only_in_the_assistants_own_turn(self) -> None:
        data = {
            "messages": [
                {"role": "user", "content": [{"type": "thinking", "thinking": "on prod-eu-1", "signature": "s"}]},
                {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "opaque", "note": "on prod-eu-1"}]},
            ]
        }
        user, assistant = self._run(data, "anthropic_messages")["messages"]
        self.assertNotIn("prod-eu-1", user["content"][0]["thinking"])
        self.assertEqual(assistant["content"][0]["data"], "opaque")
        self.assertNotIn("prod-eu-1", assistant["content"][0]["note"])
        gemini = {"contents": [{"role": "user", "parts": [{"text": "x", "thoughtSignature": _RULE_MATCHING_BASE64}]}]}
        sent = self._run(gemini, "agenerate_content")["contents"][0]["parts"][0]
        self.assertNotEqual(sent["thoughtSignature"], _RULE_MATCHING_BASE64)

    def test_a_payload_that_is_not_the_media_it_claims_is_refused(self) -> None:
        import base64 as b64

        text_as_png = b64.b64encode(b"on prod-eu-1, token here").decode()
        for part in (
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": text_as_png}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + text_as_png}},
            {"type": "image_url", "image_url": {"url": "data:image/png;name=on-prod-eu-1;base64," + _PNG_BASE64}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo!"}},  # not base64
            {"type": "image", "source": {"type": "base64", "media_type": "video/mp4", "data": text_as_png}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/heic", "data": text_as_png}},
            {"type": "input_audio", "input_audio": {"format": "ogg", "data": text_as_png}},
            {"type": "image", "source": {"type": "base64", "media_type": "audio/x-made-up", "data": _WAV_BASE64}},
        ):
            with self.assertRaises(HTTPException, msg=json.dumps(part)):
                self._run({"messages": [{"role": "user", "content": [part]}]})

    def test_json_in_tool_arguments_is_decoded_and_tool_output_is_redacted(self) -> None:
        # A value right after an escaped newline sits after the letter `n` in
        # the encoded string, where a word-boundary rule cannot see it; the
        # decoded document has a real newline there.
        args = json.dumps({"command": "kubectl get pods\n10.0.0.5", "cluster": "prod-eu-1"})
        output = json.dumps({"items": [{"status": {"podIP": "10.0.0.6"}}], "note": "on\nprod-eu-1"})
        data = {
            "messages": [
                {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "type": "function", "function": {"name": "t", "arguments": args}}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "u", "name": "Bash", "input": args}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "u", "content": output}]},
            ]
        }
        result = self._run(data, "anthropic_messages")["messages"]
        for sent in (result[0]["tool_calls"][0]["function"]["arguments"], result[1]["content"][0]["input"], result[2]["content"][0]["content"]):
            self.assertIsInstance(sent, str)
            decoded = json.loads(sent)
            text = json.dumps(decoded)
            for value in ("10.0.0.5", "10.0.0.6", "prod-eu-1"):
                self.assertNotIn(value, text)

    def test_tool_arguments_with_a_repeated_key_are_re_encoded(self) -> None:
        # The decoder keeps the last value, so the first never reaches the
        # walk: the arguments are re-encoded from the document, not sent as
        # they came.
        repeated = '{"cmd": "get pods\\n10.0.0.7", "cmd": "safe"}'
        data = {"messages": [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "t", "arguments": repeated}},
        ]}]}
        sent = self._run(data)["messages"][0]["tool_calls"][0]["function"]["arguments"]
        self.assertNotIn("10.0.0.7", sent)
        self.assertEqual(json.loads(sent), {"cmd": "safe"})

    def test_tool_arguments_with_nothing_to_redact_keep_their_exact_bytes(self) -> None:
        untouched = '{"command":   "kubectl get pods",\n "n": 1}'
        data = {"messages": [{"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "t", "arguments": untouched}}]}]}
        self.assertEqual(self._run(data)["messages"][0]["tool_calls"][0]["function"]["arguments"], untouched)

    def test_a_numeric_value_under_a_credential_key_stays_valid_json(self) -> None:
        arguments = json.dumps({"token": 98765, "user": "admin"})
        data = {"messages": [{"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": arguments}}]}]}
        sent = self._run(data)["messages"][0]["tool_calls"][0]["function"]["arguments"]
        json.loads(sent)

    def test_a_rule_matching_a_protocol_key_renames_no_key_and_drops_no_payload(self) -> None:
        module = _load_callback_module({"ip": {"action": "off"}, "rules": [{"name": "src", "literal": "source"}]})
        hook = module.proxy_handler_instance
        data = {"messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _PNG_BASE64}}]}]}
        part = asyncio.run(hook.async_pre_call_hook({}, None, data, "anthropic_messages"))["messages"][0]["content"][0]
        self.assertEqual(part["source"]["data"], _PNG_BASE64)

    def test_a_data_url_is_recognised_whatever_its_case_or_spacing(self) -> None:
        for url in ("DATA:text/plain;base64,dG9rZW49eGEyOQ==", " data:text/plain;base64,dG9rZW49eGEyOQ=="):
            with self.assertRaises(HTTPException, msg=url):
                self._run({"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]})

    def test_a_json_looking_string_under_a_credential_key_is_masked(self) -> None:
        data = {"messages": [{"role": "user", "content": "hi"}], "db_creds": {"password": '["plain_secret_password"]'}}
        self.assertNotIn("plain_secret_password", json.dumps(self._run(data)))

    def test_caller_data_shaped_like_an_internal_marker_is_just_data(self) -> None:
        lookalike = {"__kube_agents_json_string__": 0, "__kube_agents_json_document__": "x"}
        data = {"messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "a", "name": "t", "input": lookalike},
            {"type": "tool_use", "id": "b", "name": "t", "input": json.dumps({"host": "10.0.0.5"})},
        ]}]}
        first, second = self._run(data, "anthropic_messages")["messages"][0]["content"]
        self.assertEqual(first["input"], lookalike)
        self.assertNotIn("10.0.0.5", second["input"])

    # Fail closed.

    def test_a_request_in_a_shape_the_hook_cannot_walk_is_refused(self) -> None:
        for data in (
            {"model": "m", "query": f"token {_OAUTH_TOKEN}"},
            {"model": "m", "documents": ["on prod-eu-1"]},
            {"model": "m"},
            {"model": "m", "messages": {"role": "user", "content": "prod-eu-1"}},
            {"model": "m", "contents": "on prod-eu-1"},
            {"model": "m", "messages": ["prod-eu-1"]},
            {"model": "m", "contents": ["on prod-eu-1"]},
        ):
            with self.assertRaises(HTTPException) as raised:
                self._run(dict(data), "arerank")
            message = str(raised.exception)
            self.assertIn("refuses a request it cannot walk", message)
            # It names the keys and never a value.
            self.assertNotIn(_OAUTH_TOKEN, message)
            self.assertNotIn("prod-eu-1", message)

    def test_every_shape_the_gateway_serves_is_walked_not_refused(self) -> None:
        for call_type, data in (
            ("acompletion", {"messages": [{"role": "user", "content": "on prod-eu-1"}]}),
            ("anthropic_messages", {"system": "on prod-eu-1", "messages": [{"role": "user", "content": "hi"}]}),
            ("agenerate_content", {"contents": [{"role": "user", "parts": [{"text": "on prod-eu-1"}]}]}),
            ("aembedding", {"input": "on prod-eu-1"}),
            ("atext_completion", {"prompt": "on prod-eu-1"}),
        ):
            result = self._run(data, call_type)
            self.assertNotIn("prod-eu-1", json.dumps(result), call_type)

    def test_every_field_of_a_message_is_walked_not_just_its_content(self) -> None:
        data = {
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "the pod is 10.0.0.5",
                    "function_call": {"name": "f", "arguments": json.dumps({"password": "hunter2-example-pw"})},
                    "refusal": "not on prod-eu-1",
                    "tool_calls": [{"type": "custom", "custom": {"name": "c", "input": "project my-proj-8"}}],
                    "thinking_blocks": [{"type": "thinking", "thinking": "signed", "signature": "sig"}],
                }
            ]
        }
        message = self._run(data)["messages"][0]
        sent = json.dumps(message)
        for value in ("10.0.0.5", "hunter2-example-pw", "prod-eu-1", "my-proj-8"):
            self.assertNotIn(value, sent)
        self.assertEqual(message["thinking_blocks"], [{"type": "thinking", "thinking": "signed", "signature": "sig"}])

    def test_a_binary_payload_is_kept_wherever_it_sits_and_a_text_one_is_refused(self) -> None:
        payload = _RULE_MATCHING_BASE64
        data = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": payload}}]},
                        {"type": "input_audio", "input_audio": {"format": "wav", "data": _WAV_BASE64}},
                        {"type": "file", "file": {"filename": "r.pdf", "file_data": "data:application/pdf;base64," + _PDF_BASE64}},
                    ],
                }
            ]
        }
        result, audio, filed = self._run(data)["messages"][0]["content"]
        self.assertEqual(result["content"][0]["source"]["data"], payload)
        self.assertEqual(audio["input_audio"]["data"], _WAV_BASE64)
        self.assertEqual(filed["file"]["file_data"], "data:application/pdf;base64," + _PDF_BASE64)
        for part in (
            {"type": "image_url", "image_url": {"url": "data:text/plain;base64,b24gcHJvZC1ldS0x"}},
            {"type": "file", "file": {"file_data": "data:application/json;base64,e30="}},
            {"type": "image", "source": {"type": "base64", "media_type": "text/plain", "data": "b24="}},
        ):
            with self.assertRaises(HTTPException, msg=json.dumps(part)):
                self._run({"messages": [{"role": "user", "content": [part]}]})

    def test_a_bad_rule_file_fails_the_import_rather_than_the_request(self) -> None:
        for config in (
            {"rules": [{"name": "x", "pattern": "("}]},
            {"rules": [{"name": "x", "literal": ""}]},
            {"ip": {"action": False}},
        ):
            with self.subTest(config=config):
                with self.assertRaises(ValueError):
                    _load_callback_module(config)

    def test_the_count_line_reaches_a_stream_handler_at_info(self) -> None:
        # The proxy leaves the root logger at WARNING and installs no handler
        # for this logger, so the module has to carry its own or the one
        # operator-visible signal that a rule fired never reaches the pod log.
        logger = self.module.logger
        self.assertLessEqual(logger.level, self.module.logging.INFO)
        self.assertFalse(logger.propagate)
        streams = [
            h.stream
            for h in logger.handlers
            if isinstance(h, self.module.logging.StreamHandler)
        ]
        self.assertIn(sys.stderr, streams)

    def test_the_redactor_is_loaded_by_path_without_touching_sys_path(self) -> None:
        self.assertNotIn(str(_CHART_FILES), sys.path)
        self.assertIn(self.module.REDACTOR_MODULE_NAME, sys.modules)
        self.assertEqual(
            pathlib.Path(sys.modules[self.module.REDACTOR_MODULE_NAME].__file__).resolve(),
            _CHART_REDACTOR.resolve(),
        )

    def test_a_missing_config_variable_fails_the_import(self) -> None:
        _stub_litellm()
        previous = os.environ.pop(_CONFIG_ENV_VAR, None)
        try:
            spec = importlib.util.spec_from_file_location("litellm_redaction_callback", _CALLBACK)
            module = importlib.util.module_from_spec(spec)
            with self.assertRaises(RuntimeError):
                spec.loader.exec_module(module)
        finally:
            if previous is not None:
                os.environ[_CONFIG_ENV_VAR] = previous


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestChartRender(unittest.TestCase):
    @staticmethod
    def _render(values: dict | None = None, extra_args: list[str] | None = None):
        command = list(_HELM_BASE_ARGS)
        values_path = None
        if values is not None:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
                yaml.safe_dump(values, handle)
                values_path = handle.name
            command += ["-f", values_path]
        command += extra_args or []
        try:
            return subprocess.run(command, capture_output=True, text=True, check=False)
        finally:
            if values_path:
                os.unlink(values_path)

    def _documents(self, values: dict | None = None):
        proc = self._render(values)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        documents = [d for d in yaml.safe_load_all(proc.stdout) if d]
        by_kind = {d["kind"]: d for d in documents if d["kind"] in ("ConfigMap", "Deployment")}
        return by_kind["ConfigMap"], by_kind["Deployment"]

    def test_disabled_renders_none_of_it(self) -> None:
        configmap, deployment = self._documents()
        self.assertEqual(sorted(configmap["data"]), sorted(_ALWAYS_KEYS))
        self.assertNotIn(_CALLBACK_INSTANCE, configmap["data"]["config.yaml"])
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(sorted(_configmap_mounts(container)), sorted(_ALWAYS_KEYS))
        env_names = [e["name"] for e in container["env"]]
        self.assertNotIn(_CONFIG_ENV_VAR, env_names)
        self.assertNotIn(_SALT_ENV_VAR, env_names)

    def test_enabled_renders_the_hook_its_module_and_its_rule_file_beside_the_config(self) -> None:
        configmap, deployment = self._documents(
            {"litellm": {"redaction": {"enabled": True, **_RULES_CONFIG}}}
        )
        data = configmap["data"]
        self.assertEqual(sorted(data), sorted(_ALWAYS_KEYS + _CONFIGMAP_KEYS))
        # The mounted modules are the checked-in files, not a re-indented copy.
        self.assertEqual(data["redactor.py"], _CHART_REDACTOR.read_text())
        self.assertEqual(data["litellm_redaction_callback.py"], _CALLBACK.read_text())
        self.assertEqual(yaml.safe_load(data["redaction.yaml"]), _RULES_CONFIG)
        settings = yaml.safe_load(data["config.yaml"])["litellm_settings"]
        self.assertEqual(settings["callbacks"], _ALWAYS_CALLBACKS + [_CALLBACK_INSTANCE])

        container = deployment["spec"]["template"]["spec"]["containers"][0]
        mounts = _configmap_mounts(container)
        for key in ("config.yaml",) + _CONFIGMAP_KEYS:
            self.assertEqual(mounts.get(key), f"{_MOUNT_DIR}/{key}")
        env = {e["name"]: e for e in container["env"]}
        self.assertEqual(env[_CONFIG_ENV_VAR]["value"], f"{_MOUNT_DIR}/redaction.yaml")
        salt = env[_SALT_ENV_VAR]["valueFrom"]["secretKeyRef"]
        self.assertEqual(salt["key"], _SALT_ENV_VAR)
        self.assertTrue(salt["optional"])

    def test_the_checksum_covers_the_rule_file(self) -> None:
        def checksum(values):
            _, deployment = self._documents(values)
            return deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

        base = {"litellm": {"redaction": {"enabled": True, **_RULES_CONFIG}}}
        changed = {"litellm": {"redaction": {"enabled": True, **_RULES_CONFIG, "ip": {"action": "mask"}}}}
        self.assertNotEqual(checksum(base), checksum(changed))

    def test_an_unknown_action_fails_the_render(self) -> None:
        for override, needle in (
            ("litellm.redaction.ip.action=reverse", "litellm.redaction.ip.action"),
            ("litellm.redaction.rules[0].action=hash", "litellm.redaction.rules[0]"),
            ("litellm.redaction.rules[1].literal=x", "exactly one of pattern or literal"),
            ("litellm.redaction.rules[1].name=-project", "must start with a letter or digit"),
        ):
            with self.subTest(override=override):
                proc = self._render(
                    {"litellm": {"redaction": {"enabled": True, **_RULES_CONFIG}}},
                    ["--set", override],
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(needle, proc.stderr)

    def test_a_values_file_may_turn_ip_redaction_off_only_with_a_quoted_string(self) -> None:
        # YAML 1.1 reads a bare `off` as boolean false, which `default` would
        # have turned into pseudonym without a word said. The values file is
        # written as text so the bare spelling reaches Helm's loader unquoted.
        def render_action(spelling: str):
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
                handle.write(
                    "litellm:\n  redaction:\n    enabled: true\n    ip:\n"
                    f"      action: {spelling}\n"
                )
                values_path = handle.name
            try:
                return subprocess.run(
                    _HELM_BASE_ARGS + ["-f", values_path],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            finally:
                os.unlink(values_path)

        bare = render_action("off")
        self.assertNotEqual(bare.returncode, 0)
        self.assertIn('quote it: action: "off"', bare.stderr)

        quoted = render_action('"off"')
        self.assertEqual(quoted.returncode, 0, quoted.stderr)
        configmap = next(
            d for d in yaml.safe_load_all(quoted.stdout) if d and d["kind"] == "ConfigMap"
        )
        self.assertEqual(yaml.safe_load(configmap["data"]["redaction.yaml"])["ip"]["action"], "off")

    def test_a_rule_source_that_is_not_a_non_empty_string_fails_the_render(self) -> None:
        for rule, needle in (
            ({"name": "x", "literal": ""}, "must be a non-empty string"),
            ({"name": "x", "literal": True}, "must be a non-empty string"),
            ({"name": "x", "pattern": None}, "must be a non-empty string"),
            ({"name": "x", "literal": 1.1}, "must be a non-empty string"),
            ({"name": "x", "literal": "a", "action": False}, "action must be a string"),
        ):
            with self.subTest(rule=rule):
                proc = self._render({"litellm": {"redaction": {"enabled": True, "rules": [rule]}}})
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(needle, proc.stderr)


class TestCompositionWiring(unittest.TestCase):
    """terraform/examples/full-install's litellm_redaction, read as text (the
    terraform binary is not a suite dependency). Its validations fail the plan
    on what the chart would fail the helm_release apply on, so each accepted
    set is pinned to the chart's own."""

    _COMPOSITION = _REPO_ROOT / "terraform" / "examples" / "full-install"

    def setUp(self) -> None:
        variables = (self._COMPOSITION / "variables.tf").read_text()
        start = variables.index('variable "litellm_redaction" {')
        self.variable = variables[start : variables.index("\n}\n", start)]
        self.main_tf = (self._COMPOSITION / "main.tf").read_text()
        self.chart = (_CHART / "templates" / "litellm.yaml").read_text()

    def test_the_variable_accepts_what_the_chart_accepts(self) -> None:
        for chart_literal, variable_literal in (
            ('(list "mask" "pseudonym" "off")', 'contains(["mask", "pseudonym", "off"], var.litellm_redaction.ip_action)'),
            ('(list "mask" "pseudonym")', 'contains(["mask", "pseudonym"], r.action == null ? "mask" : r.action)'),
            ("`^[A-Za-z0-9][A-Za-z0-9_.\\-]*$`", 'regex("^[A-Za-z0-9][A-Za-z0-9_.-]*$", r.name)'),
        ):
            with self.subTest(chart_literal=chart_literal):
                self.assertIn(chart_literal, self.chart)
                self.assertIn(variable_literal, self.variable)

    def test_every_validation_applies_only_while_enabled(self) -> None:
        # The chart reads none of these while redaction is off, so a leftover
        # value must not stop a plan, an upgrade or a destroy.
        conditions = [line for line in self.variable.splitlines() if line.strip().startswith("condition")]
        self.assertEqual(len(conditions), 5)
        for line in conditions:
            with self.subTest(line=line):
                self.assertIn("condition     = !var.litellm_redaction.enabled || ", line)

    def test_redaction_reaches_the_chart_only_while_enabled_without_null_rule_keys(self) -> None:
        # Off, the litellm values must not change, or every existing install's
        # gateway rolls on upgrade; a null rule key fails the chart's checks.
        self.assertIn("var.litellm_redaction.enabled ? {\n        redaction = {", self.main_tf)
        # The chart value names, which the chart reads with defaults: a
        # misspelt key would drop the setting without an error.
        for line in (
            "enabled = true",
            "action     = var.litellm_redaction.ip_action",
            "allowCidrs = var.litellm_redaction.allow_cidrs",
        ):
            self.assertIn(line, self.main_tf)
        self.assertIn('(dict "ip" (dict "action" $ipAction "allowCidrs" ($ip.allowCidrs', self.chart)
        self.assertIn(
            "rules = [for r in var.litellm_redaction.rules : { for k, v in r : k => v if v != null }]",
            self.main_tf,
        )


if __name__ == "__main__":
    unittest.main()
