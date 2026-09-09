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
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

import yaml

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
_MOUNT_DIR = "/app"
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
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        data = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "node 10.0.0.9"}, image]}
            ]
        }
        parts = self._run(data)["messages"][0]["content"]
        self.assertRegex(parts[0]["text"], r"^node \[ip:[0-9a-f]{12}\]$")
        self.assertEqual(parts[1], image)

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

    def test_a_bad_rule_file_fails_the_import_rather_than_the_request(self) -> None:
        with self.assertRaises(ValueError):
            _load_callback_module({"rules": [{"name": "x", "pattern": "("}]})

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
        self.assertEqual(sorted(configmap["data"]), ["config.yaml"])
        self.assertNotIn(_CALLBACK_INSTANCE, configmap["data"]["config.yaml"])
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual([m["subPath"] for m in container["volumeMounts"]], ["config.yaml"])
        env_names = [e["name"] for e in container["env"]]
        self.assertNotIn(_CONFIG_ENV_VAR, env_names)
        self.assertNotIn(_SALT_ENV_VAR, env_names)

    def test_enabled_renders_the_hook_its_module_and_its_rule_file_beside_the_config(self) -> None:
        configmap, deployment = self._documents(
            {"litellm": {"redaction": {"enabled": True, **_RULES_CONFIG}}}
        )
        data = configmap["data"]
        self.assertEqual(sorted(data), sorted(("config.yaml",) + _CONFIGMAP_KEYS))
        # The mounted modules are the checked-in files, not a re-indented copy.
        self.assertEqual(data["redactor.py"], _CHART_REDACTOR.read_text())
        self.assertEqual(data["litellm_redaction_callback.py"], _CALLBACK.read_text())
        self.assertEqual(yaml.safe_load(data["redaction.yaml"]), _RULES_CONFIG)
        settings = yaml.safe_load(data["config.yaml"])["litellm_settings"]
        self.assertEqual(settings["callbacks"], ["prometheus", _CALLBACK_INSTANCE])

        container = deployment["spec"]["template"]["spec"]["containers"][0]
        mounts = {m["subPath"]: m["mountPath"] for m in container["volumeMounts"]}
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
        ):
            with self.subTest(override=override):
                proc = self._render(
                    {"litellm": {"redaction": {"enabled": True, **_RULES_CONFIG}}},
                    ["--set", override],
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(needle, proc.stderr)


if __name__ == "__main__":
    unittest.main()
