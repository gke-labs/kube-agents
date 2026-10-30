"""Tests for litellm.cacheProbeEnabled and the cache probe callback.

1. The chart: off by default with the default render unchanged; on, the
   ConfigMap carries the file verbatim, the callback is registered, the file is
   mounted beside config.yaml and the pods roll; the schema takes booleans only.
2. The eval install turns it on (hack/ci-deploy.sh).
3. The callback: one "cache probe:" line per call, holding hashes, counts and
   usage and none of the prompt's text; the hashes move only with what they
   cover; a payload it cannot read costs one warning and never raises.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import pathlib
import shutil
import subprocess
import unittest

import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CHART = _REPO_ROOT / "charts" / "kube-agents"
_CHART_FILE = _CHART / "files" / "litellm_cache_probe_callback.py"
_CI_DEPLOY = _REPO_ROOT / "hack" / "ci-deploy.sh"
_CALLBACK = "litellm_cache_probe_callback.proxy_handler_instance"
_KEY = "litellm_cache_probe_callback.py"
_MOUNT = "/app/litellm_cache_probe_callback.py"
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

_SYSTEM_SECRET_PHRASE = "you are the platform agent of ACME-SYSTEM-MARKER"
_USER_SECRET_PHRASE = "fix the crashloop in ACME-USER-MARKER"
_TOOL_SECRET_PHRASE = "ACME-TOOL-MARKER"


def _load_module():
    spec = importlib.util.spec_from_file_location("litellm_cache_probe_callback", _CHART_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {"name": name, "description": _TOOL_SECRET_PHRASE, "parameters": {"type": "object"}},
    }


def _kwargs(system: object, tools: list | None = None, turns: int = 1) -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": _USER_SECRET_PHRASE}]
    for index in range(turns - 1):
        messages.append({"role": "assistant", "content": f"step {index}"})
    return {
        "model": "model-default",
        "messages": messages,
        "optional_params": {"tools": tools if tools is not None else [_tool("kubectl")], "reasoning_effort": "high"},
        "litellm_params": {"model": "vertex_ai/claude-opus-5-5"},
    }


def _response(**usage: object) -> dict:
    return {"usage": usage}


@unittest.skipUnless(shutil.which("helm"), "helm is not installed")
class TestCacheProbeChartRender(unittest.TestCase):
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

    def test_default_unset_and_explicit_false_are_identical_and_carry_no_probe(self) -> None:
        unset_cm, unset_deploy = self._documents()
        false_cm, false_deploy = self._documents(["--set", "litellm.cacheProbeEnabled=false"])

        self.assertEqual(unset_cm, false_cm)
        self.assertEqual(self._checksum(unset_deploy), self._checksum(false_deploy))
        self.assertNotIn(_KEY, unset_cm["data"])
        self.assertNotIn(_CALLBACK, self._callbacks(unset_cm))
        self.assertFalse(any(m.get("mountPath") == _MOUNT for m in self._mounts(unset_deploy)))

    def test_enabled_renders_file_callback_mount_and_rolls_the_pods(self) -> None:
        set_cm, set_deploy = self._documents(["--set", "litellm.cacheProbeEnabled=true"])
        _, unset_deploy = self._documents()

        self.assertEqual(set_cm["data"][_KEY], _CHART_FILE.read_text(encoding="utf-8"))
        callbacks = self._callbacks(set_cm)
        self.assertIn(_CALLBACK, callbacks)
        self.assertIn("litellm_thinking_replay_callback.proxy_handler_instance", callbacks)
        mount = next(m for m in self._mounts(set_deploy) if m.get("mountPath") == _MOUNT)
        self.assertEqual(mount["subPath"], _KEY)
        self.assertEqual(mount["name"], "config-volume")
        self.assertNotEqual(self._checksum(unset_deploy), self._checksum(set_deploy))

    def test_schema_takes_booleans_only(self) -> None:
        proc = subprocess.run(
            _HELM_BASE_ARGS + ["--set-string", "litellm.cacheProbeEnabled=yes"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("cacheProbeEnabled", proc.stderr)


class TestCacheProbeEvalSetting(unittest.TestCase):
    def test_the_eval_install_turns_the_probe_on(self) -> None:
        self.assertIn('--set "litellm.cacheProbeEnabled=true"', _CI_DEPLOY.read_text(encoding="utf-8"))


class TestCacheProbeRecord(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_module()

    def _line(self, kwargs: dict, response: object) -> tuple[str, dict]:
        with self.assertLogs(self.mod.logger, level=logging.INFO) as captured:
            asyncio.run(self.mod.proxy_handler_instance.async_log_success_event(kwargs, response, None, None))
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertTrue(message.startswith(self.mod.LOG_PREFIX), message)
        return message, json.loads(message[len(self.mod.LOG_PREFIX) :])

    def test_line_holds_hashes_counts_and_usage_and_no_prompt_text(self) -> None:
        message, record = self._line(
            _kwargs(_SYSTEM_SECRET_PHRASE, turns=3),
            _response(
                prompt_tokens=61000,
                completion_tokens=300,
                cache_creation_input_tokens=60000,
                cache_read_input_tokens=0,
                cache_creation_token_details={"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 60000},
            ),
        )
        for phrase in (_SYSTEM_SECRET_PHRASE, _USER_SECRET_PHRASE, _TOOL_SECRET_PHRASE, "kubectl"):
            self.assertNotIn(phrase, message)
        self.assertEqual(record["msgs"], 4)
        self.assertEqual(record["tools"], 1)
        self.assertEqual(record["system_chars"], len(_SYSTEM_SECRET_PHRASE))
        self.assertEqual(record["model"], "vertex_ai/claude-opus-5-5")
        self.assertEqual(record["reasoning_effort"], "high")
        self.assertEqual(record["prompt_tokens"], 61000)
        self.assertEqual(record["cache_creation_input_tokens"], 60000)
        self.assertEqual(record["cache_read_input_tokens"], 0)
        self.assertEqual(record["ephemeral_1h_input_tokens"], 60000)
        self.assertEqual(record["ephemeral_5m_input_tokens"], 0)

    def test_the_ttl_split_is_read_from_prompt_tokens_details_too(self) -> None:
        _, record = self._line(
            _kwargs("s"),
            _response(
                prompt_tokens=100,
                prompt_tokens_details={
                    "cached_tokens": 0,
                    "cache_creation_token_details": {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 60},
                },
            ),
        )
        self.assertEqual(record["ephemeral_5m_input_tokens"], 40)
        self.assertEqual(record["ephemeral_1h_input_tokens"], 60)

    def test_thinking_logs_its_type_and_budget_only(self) -> None:
        kwargs = _kwargs("s")
        kwargs["optional_params"]["thinking"] = {"type": "enabled", "budget_tokens": 4096}
        self.assertEqual(self.mod.probe_record(kwargs, {})["thinking"], {"type": "enabled", "budget_tokens": 4096})
        self.assertIsNone(self.mod.probe_record(_kwargs("s"), {})["thinking"])

    def test_usage_is_read_from_an_object_response(self) -> None:
        class Usage:
            def model_dump(self):
                return {"prompt_tokens": 10, "cache_read_input_tokens": 8}

        class Response:
            usage = Usage()

        _, record = self._line(_kwargs("s"), Response())
        self.assertEqual(record["prompt_tokens"], 10)
        self.assertEqual(record["cache_read_input_tokens"], 8)
        self.assertNotIn("cache_creation_input_tokens", record)

    def test_one_session_keeps_its_conv_and_its_message_count_grows(self) -> None:
        first = self.mod.probe_record(_kwargs("s", turns=1), {})
        later = self.mod.probe_record(_kwargs("s", turns=5), {})
        self.assertEqual(first["conv"], later["conv"])
        self.assertLess(first["msgs"], later["msgs"])

    def test_cache_control_markers_do_not_move_the_system_hash(self) -> None:
        plain = self.mod.probe_record(_kwargs("the system prompt"), {})
        marked = self.mod.probe_record(
            _kwargs([{"type": "text", "text": "the system prompt", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]),
            {},
        )
        self.assertEqual(plain["system_hash"], marked["system_hash"])
        self.assertEqual(plain["system_chunks"], marked["system_chunks"])

    def test_a_change_in_the_tail_leaves_the_earlier_chunks_alone(self) -> None:
        size = self.mod.SYSTEM_CHUNK_CHARS
        stable = "a" * (size * 2)
        before = self.mod.probe_record(_kwargs(stable + "Today is Monday"), {})
        after = self.mod.probe_record(_kwargs(stable + "Today is Tuesday, longer"), {})
        before_chunks = before["system_chunks"].split(self.mod.CHUNK_SEPARATOR)
        after_chunks = after["system_chunks"].split(self.mod.CHUNK_SEPARATOR)
        self.assertEqual(len(before_chunks), 3)
        self.assertEqual(before_chunks[:2], after_chunks[:2])
        self.assertNotEqual(before_chunks[2], after_chunks[2])
        self.assertNotEqual(before["system_hash"], after["system_hash"])
        self.assertEqual(before["tools_hash"], after["tools_hash"])

    def test_tools_hash_moves_with_the_tool_list_and_not_with_key_order(self) -> None:
        base = self.mod.probe_record(_kwargs("s", tools=[_tool("kubectl")]), {})
        reordered_tool = {"function": _tool("kubectl")["function"], "type": "function"}
        reordered = self.mod.probe_record(_kwargs("s", tools=[reordered_tool]), {})
        grown = self.mod.probe_record(_kwargs("s", tools=[_tool("kubectl"), _tool("gcloud")]), {})
        self.assertEqual(base["tools_hash"], reordered["tools_hash"])
        self.assertNotEqual(base["tools_hash"], grown["tools_hash"])
        self.assertEqual(grown["tools"], 2)
        self.assertEqual(base["system_hash"], grown["system_hash"])

    def test_a_request_without_tools_or_system_still_logs(self) -> None:
        record = self.mod.probe_record({"messages": [{"role": "user", "content": "hi"}]}, None)
        self.assertEqual(record["tools"], 0)
        self.assertEqual(record["system_chars"], 0)
        self.assertEqual(record["system_chunks"], "")
        self.assertIsNotNone(record["conv"])

    def test_an_unreadable_payload_warns_once_and_never_raises(self) -> None:
        bad = {"messages": object()}
        with self.assertLogs(self.mod.logger, level=logging.WARNING) as captured:
            for _ in range(3):
                asyncio.run(self.mod.proxy_handler_instance.async_log_success_event(bad, None, None, None))
        self.assertEqual(len(captured.records), 1)
        message = captured.records[0].getMessage()
        self.assertTrue(message.startswith(self.mod.FAILURE_PREFIX), message)
        self.assertFalse(message.startswith(self.mod.LOG_PREFIX), message)


if __name__ == "__main__":
    unittest.main()
